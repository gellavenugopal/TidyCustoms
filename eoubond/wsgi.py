"""WSGI entry point for production servers: gunicorn eoubond.wsgi:app"""
from .web import create_app

app = create_app()
