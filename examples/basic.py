from pathlib import Path

from pykrozen import Server, file, get, html, settings, static

BASE_DIR = Path(__file__).parent

static("/static")


@get("/")
def home(req):
    return file(BASE_DIR / "index.html")


@get("/hello")
def hello(req):
    return html("<h1>Hello World</h1>")


settings.debug = True
settings.base_dir = BASE_DIR
server = Server.run()
