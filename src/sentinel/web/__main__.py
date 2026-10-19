import uvicorn

from sentinel.config import load_settings_or_exit


def main() -> None:
    settings = load_settings_or_exit()
    uvicorn.run(
        "sentinel.web.app:create_app",
        factory=True,
        host=settings.web_host,
        port=settings.web_port,
        reload=settings.web_reload,
        server_header=False,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
