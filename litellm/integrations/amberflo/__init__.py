class AmberfloLogger:

    __name__ = "amberflo"

    def __init__(self):
        # lazy load internal modules
        from .litellm import callback

        self.callback = callback

    async def __call__(self, *args, **kwargs) -> None:
        await self.callback(*args, **kwargs)

    async def async_post_call_success_hook(self, *args, **kwargs) -> None:
        """
        This is needed by LiteLLM.
        """
        pass

