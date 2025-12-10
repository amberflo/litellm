# What is this?
## On Success events log usage to Amberflo


class AmberfloLogger:

    __name__ = "amberflo"

    async def __call__(self, *args, **kwargs) -> None:
        print("## callback 1 ##", flush=True)
