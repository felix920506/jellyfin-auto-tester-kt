"""CLI entry point for the Jellyfin auto-tester pipeline."""


async def run_issue(issue_url: str, container_version: str) -> None:
    from kohaku_terrarium import Terrarium

    engine = await Terrarium.from_recipe("terrarium.yaml")
    analysis = engine["analysis_agent"]
    async for chunk in analysis.chat(
        f"Issue: {issue_url}\nTarget version: {container_version}"
    ):
        print(chunk, end="")

