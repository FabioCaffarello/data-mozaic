"""Fixed technical defaults for the Scrapy Collector project."""

BOT_NAME = "mozaic_scrapy_collector"
SPIDER_MODULES = ["scrapy_collector.spiders.generated"]
NEWSPIDER_MODULE = "scrapy_collector.spiders.generated"

LOG_ENABLED = False
TELNETCONSOLE_ENABLED = False
COOKIES_ENABLED = False
RETRY_ENABLED = False
REDIRECT_ENABLED = False
METAREFRESH_ENABLED = False
FEEDS = {}
FEED_EXPORTERS = {}
JOBDIR = None

# BotBuild-specific fixture handlers and pipelines are verified and applied by
# the selected BotBuild's closed settings map. The project never accepts
# settings overrides from a run request.
