# -*- coding: utf-8 -*-
"""最小宿主替身：只为了让 backend/tool.py 能在无 calibre 的机器上 import。

只模拟 import 期需要的形状（BaseTool、BaseHandler、js/is_admin、i18n._），
不模拟请求处理；宿主真实实现见 mybooks源码/mybooks-latest/webserver/toolbox/base_tool.py。
"""
import sys
import types


def _module(name):
    module = types.ModuleType(name)
    sys.modules[name] = module
    return module


def install():
    if "webserver" in sys.modules:
        return

    webserver = _module("webserver")
    handlers = _module("webserver.handlers")
    handlers_base = _module("webserver.handlers.base")
    toolbox = _module("webserver.toolbox")
    base_tool = _module("webserver.toolbox.base_tool")
    i18n = _module("webserver.i18n")

    class BaseHandler(object):
        pass

    def js(func):
        return func

    def is_admin(func):
        return func

    handlers_base.BaseHandler = BaseHandler
    handlers_base.js = js
    handlers_base.is_admin = is_admin

    class BaseTool(object):
        TOOL_DATA_ROOT = "/tmp/mybooks-toolbox"
        TOOL_SERVICE_TYPE = None
        service_item_name = ""

        def __init__(self):
            self.api = None

        @staticmethod
        def info():
            raise NotImplementedError

        @classmethod
        def tool_id(cls):
            return cls.info()["tool_id"]

        def create_task(self, progress_data=None):
            return 1

        def update_task_progress(self, task_id, progress, progress_data=None):
            return None

        def complete_task(self, task_id, error_message=None):
            return None

    base_tool.BaseTool = BaseTool

    def _(message):
        return message

    i18n._ = _

    webserver.handlers = handlers
    webserver.toolbox = toolbox
    handlers.base = handlers_base
    toolbox.base_tool = base_tool
