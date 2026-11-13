import os
import logging
import uvicorn

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ANIZONEFLIX_MAIN")

if __name__ == "__main__":
    from config.config import Config
    port = Config.PORT
    logger.info(f"Starting Web Server on port {port}...")
    try:
        # We use string import for app to avoid issues with event loops
        # and to allow uvicorn to handle the application correctly.
        uvicorn.run("app:app", host="0.0.0.0", port=port, log_level="info")
    except (KeyboardInterrupt, SystemExit):
        logger.info("System shutting down...")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
