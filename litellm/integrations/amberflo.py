import asyncio
import json
import os
from typing import Optional

import httpx

from litellm._logging import verbose_logger
from litellm.llms.custom_httpx.http_handler import (
    get_async_httpx_client,
    httpxSpecialProvider,
)


def _parse_boolean(value):
    return value.lower() in {"true", "yes"}


class AmberfloLogger:

    __name__ = "amberflo"

    def __init__(self):
        self.send_metadata = _parse_boolean(os.getenv("AFLO_SEND_OBJECT_METADATA", "true"))
        self.hosted_env = os.getenv("AFLO_HOSTED_ENV", "unknown")
        self.endpoint = os.getenv("AFLO_API_ENDPOINT", "https://ingest.amberflo.io")

        api_key = os.getenv("AFLO_API_KEY")
        if not api_key:
            raise Exception("Missing AFLO_API_KEY")

        self.headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key,
        }

        self.async_client = get_async_httpx_client(
            llm_provider=httpxSpecialProvider.LoggingCallback
        )

    async def async_post_call_success_hook(self, *args, **kwargs) -> None:
        """
        This is needed by LiteLLM.
        """
        pass

    async def __call__(self, *args, **kwargs) -> None:
        log = None
        if args and isinstance(args[0], dict):
            log = args[0].get("standard_logging_object")

        if log:
            self._handle_log_object(log)

    def _handle_log_object(self, log):
        try:
            events = extract_events_from_log(
                log,
                send_metadata=self.send_metadata,
                hosted_env=self.hosted_env,
            )
            if events:
                # Avoid blocking
                asyncio.create_task(self._async_write(events))
        except Exception as e:
            verbose_logger.exception(f"Amberflo: failed extracting events {e}")

    async def _async_write(self, events):
        response: Optional[httpx.Response] = None
        try:
            response = await self.async_client.post(
                url=self.endpoint,
                data=json.dumps(events),
                headers=self.headers,
            )
            response.raise_for_status()
            verbose_logger.debug("Amberflo: sent events")

        except Exception as e:
            verbose_logger.exception(f"Amberflo: failed sending events: {e}")


_unknown = "unknown"
_global = "global"
_n = "n"


def extract_events_from_log(log, send_metadata, hosted_env):
    metadata = log["metadata"]

    request_id = log["id"]
    request_time_ms = round(log["startTime"] * 1000)
    request_duration_ms = round((log["endTime"] - log["startTime"]) * 1000)

    business_unit_id, team = _get_bu_and_team(metadata)

    provider, model = _resolve_provider_model(log)

    model_info = log["model_map_information"]["model_map_value"]

    if model_info:
        sku = model_info["key"]
        platform = model_info["litellm_provider"]
    else:
        # happens on error scenarios
        sku = _unknown
        platform = _unknown

    batch = "y" if log["hidden_params"]["batch_models"] else _n

    usecase = log["call_type"]

    key_name = metadata.get("user_api_key_alias") or _unknown

    user = metadata.get("user_api_key_user_id") or _unknown

    region = _resolve_region(platform, log) or _global

    ## ERRORS
    error_details = _extract_error_details(log["error_information"])

    error_code = error_details["code"] if error_details else _n

    usage = _extract_usage(usecase, metadata["usage_object"], log["model_parameters"])

    # TODO implement "tier"
    tier = _n

    if send_metadata:
        metadata_events = [
            {
                "meterApiName": "aflo.object_metadata",
                "meterValue": 1,
                "meterTimeInMillis": request_time_ms,
                "dimensions": obj,
            }
            for obj in _get_object_metadata(metadata)
        ]
    else:
        metadata_events = []

    pricing_dimensions = {
        "sku": sku,
        "tier": tier,
        "batch": batch,
    }

    dimensions = {
        "business_unit_id": business_unit_id,
        "team": team,
        "hosted_env": hosted_env,
        "key_name": key_name,
        "model": model,
        "platform": platform,
        "provider": provider,
        "region": region,
        "usecase": usecase,
        "user": user,
        "gateway": "litellm",
    }

    base_event = {
        "meterTimeInMillis": request_time_ms,
        "uniqueId": request_id,
    }

    events = metadata_events + [
        {
            **base_event,
            "meterApiName": "llm_api_call",
            "meterValue": 1,
            "dimensions": {
                **dimensions,
                **pricing_dimensions,
                "error_code": error_code,
            },
        },
        {
            **base_event,
            "meterApiName": "llm_api_call_ms",
            "meterValue": request_duration_ms,
            "dimensions": {
                **dimensions,
                **pricing_dimensions,
                "error_code": error_code,
            },
        },
    ]

    for unit, quantity, in_out, cache in usage:
        events.append(
            {
                **base_event,
                "meterApiName": _get_meter_name(unit),
                "meterValue": quantity,
                "dimensions": {
                    **dimensions,
                    **pricing_dimensions,
                    "type": in_out,
                    "cache": cache,
                },
            }
        )

    if error_details:
        events.append(
            {
                **base_event,
                "meterApiName": "llm_error_details",
                "meterValue": 1,
                "dimensions": {**dimensions, **error_details},
            }
        )

    return events


def _resolve_region(platform, log):
    if platform == "bedrock":
        return _get_api_base_domain_part(log, 1)

    # TODO test these
    if platform in ("azure", "google"):
        return _get_api_base_domain_part(log, 0)

    return None


def _get_bu_and_team(metadata):
    bu_id = metadata.get("user_api_key_auth_metadata", {}).get("business_unit_id")
    team_id = metadata.get("user_api_key_team_id")

    return bu_id or team_id or _unknown, team_id or _unknown


def _get_object_metadata(metadata):
    bu_id = metadata.get("user_api_key_auth_metadata", {}).get("business_unit_id")
    team_id = metadata.get("user_api_key_team_id")

    if bu_id and team_id:
        team_alias = metadata.get("user_api_key_team_alias")

        return [
            {
                "type": "virtual_tag",
                "name": "team",
                "value": team_id,
                "label": team_alias,
                "parentName": "businessUnitId",
                "parentValue": bu_id,
            }
        ]

    if bu_id:
        return [
            {
                "type": "business_unit",
                "id": bu_id,
                "name": bu_id,
            }
        ]

    if team_id:
        team_alias = metadata.get("user_api_key_team_alias")

        return [
            {
                "type": "business_unit",
                "id": team_id,
                "name": team_alias,
            },
            {
                "type": "virtual_tag",
                "name": "team",
                "value": team_id,
                "label": team_alias,
                "parentName": "businessUnitId",
                "parentValue": team_id,
            },
        ]

    return []


def _get_api_base_domain_part(log, index):
    api_base = log["api_base"]

    if api_base:
        hostname = urlparse(api_base).hostname
        if hostname:
            parts = hostname.split(".")
            return parts[index]

    return None


def _resolve_provider_model(log):
    provider, model = log["custom_llm_provider"], log["model"]

    if provider != "openai" and "." in model:
        provider, model = model.split(".", 1)

    return provider, model


def _get_meter_name(unit):
    if unit == "query":
        unit = "queries"

    elif unit == "token":
        unit = "text_token"

    return f"llm_{unit}s"


_openai_429_error_regex = r"Rate limit reached .* on .*\(([^:]+)\): Limit (\d+)"

_litellm_429_error_regex = (
    r"Rate limit exceeded for (\w+):.+Limit type: (\w+).+Current limit: (\d+)"
)


def _extract_error_details(error_info):
    if not error_info["error_class"]:
        return None

    error = {
        "class": error_info["error_class"],
        "code": error_info["error_code"],
    }

    if error["code"] == "429":
        message = error_info["error_message"]

        if error_info["llm_provider"] == "openai":
            match = re.search(_openai_429_error_regex, message)
            if match:
                error["subject"] = "provider"
                error["rate"] = match.group(1).lower()
                error["limit"] = match.group(2)

        else:
            match = re.search(_litellm_429_error_regex, message)
            if match:
                error["subject"] = match.group(1)

                limit_type = match.group(2)
                error["rate"] = "rpm" if limit_type == "requests" else "tpm"

                error["limit"] = match.group(3)

    return error


def _extract_usage(usecase, usage_info, model_parameters):
    """
    Returns a list of usage tuples:
        (unit, quantity, type, cache)

    TODO implement "cache"
    TODO implement and test more cases
    """
    usage = []

    prompt_tokens = usage_info.get("prompt_tokens") or 0
    comp_tokens = usage_info.get("completion_tokens") or 0

    if usecase == "aimage_generation":
        if model_parameters:
            n = model_parameters.get("n", 1)
        else:
            n = 1

        usage.append(("token", prompt_tokens, "in", _n))
        usage.append(("image", n, "out", _n))
        usage.append(("image_token", comp_tokens, "out", _n))

    else:
        usage.append(("token", prompt_tokens, "in", _n))
        usage.append(("token", comp_tokens, "out", _n))

    return (u for u in usage if u[1])
