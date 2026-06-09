# -*- coding: utf-8 -*-

import click

from rqalpha.utils.i18n import gettext as _

from .entry import cli


@cli.command(help=_("Start a local web UI for running the built-in example strategy"))
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8000, show_default=True, type=click.INT)
@click.option("--data-bundle-path", default=None, type=click.Path(), help="override RQAlpha data bundle path")
def web(host, port, data_bundle_path):
    from rqalpha.webapp import RQAlphaWebApp

    app = RQAlphaWebApp(host=host, port=port, data_bundle_path=data_bundle_path)
    app.serve_forever()
