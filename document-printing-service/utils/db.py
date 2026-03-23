from flask import current_app


def get_db():
    return current_app.extensions["mongo_db"]


def get_fs():
    return current_app.extensions["gridfs"]
