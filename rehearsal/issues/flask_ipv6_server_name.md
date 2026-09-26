`app.run()` crashes when SERVER_NAME is an IPv6 address with a port

With `app.config["SERVER_NAME"] = "[::1]:8000"`, starting the development server with `app.run()`
fails before anything is served (the host/port taken from SERVER_NAME are wrong, and converting the port
raises a ValueError). IPv6 server names, with or without a port, should give the development server the
right host and port, just like `localhost:8000` does. Explicit `host`/`port` arguments must still win.
