from client_demo import client_bp
from quart import Quart
from quart import render_template
from server_demo import server_bp

app = Quart(__name__)
app.register_blueprint(client_bp)
app.register_blueprint(server_bp)


@app.route("/")
async def landing():
    return await render_template("landing.html")
