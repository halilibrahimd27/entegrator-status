import uvicorn

from sentinel.config import load_settings_or_exit
from sentinel.web.logging import uvicorn_log_config


def main() -> None:
    settings = load_settings_or_exit()
    uvicorn.run(
        "sentinel.web.app:create_app",
        factory=True,
        host=settings.web_host,
        port=settings.web_port,
        reload=settings.web_reload,
        server_header=False,
        proxy_headers=True,
        forwarded_allow_ips=settings.web_trusted_proxies,
        log_config=uvicorn_log_config(),
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
