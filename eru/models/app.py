#!/usr/bin/python
#coding:utf-8

from werkzeug.utils import cached_property

from eru.models import db, Base
from eru.models.appconfig import AppConfig, SubAppConfig
from datetime import datetime

class Version(Base):
    __tablename__ = 'version'

    sha = db.Column(db.CHAR(40), index=True, nullable=False)
    aid = db.Column(db.Integer, db.ForeignKey('app.id'))
    created = db.Column(db.DateTime, default=datetime.now)

    containers = db.relationship('Container', backref='version', lazy='dynamic')
    tasks = db.relationship('Task', backref='version', lazy='dynamic')

    def __init__(self, app_id, sha):
        self.aid = app_id
        self.sha = sha

    @cached_property
    def name(self):
        return self.application.name

    @cached_property
    def application(self):
        return App.get(self.aid)

    @cached_property
    def appconfig(self):
        return AppConfig.get_by_name_and_version(self.name, self.short_sha)

    @property
    def short_sha(self):
        return self.sha[:7]

    def get_subapp_config(self, subname=''):
        """我也不知道干嘛做这么多容错..."""
        if subname == '' or subname == self.name:
            return self.appconfig
        return SubAppConfig.get_by_subname(self.name, subname, self.short_sha)


class App(Base):
    __tablename__ = 'app'

    name = db.Column(db.CHAR(32), nullable=False, unique=True)
    git = db.Column(db.String(255), nullable=False)
    gid = db.Column(db.Integer, db.ForeignKey('group.id'))
    token = db.Column(db.CHAR(32), nullable=False, unique=True)
    update = db.Column(db.DateTime, default=datetime.now)

    #TODO FK to more resource

    versions = db.relationship('Version', backref='app', lazy='dynamic')
    containers = db.relationship('Container', backref='app', lazy='dynamic')
    tasks = db.relationship('Task', backref='app', lazy='dynamic')
    mysql = db.relationship('MySQL', backref='app', lazy='dynamic')
    influxdb = db.relationship('InfluxDB', backref='app', lazy='dynamic')

    def __init__(self, name, git, token):
        self.name = name
        self.git = git
        self.token = token

    @classmethod
    def get(cls, id):
        return cls.query.filter(cls.id == id).one()
