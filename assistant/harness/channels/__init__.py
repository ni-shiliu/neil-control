"""渠道适配层：把各渠道输入统一转成 IncomingRequest（六层内核的入口）。"""

from harness.channels.request import IncomingRequest, RequestIdentity, create_incoming_request

__all__ = ["IncomingRequest", "RequestIdentity", "create_incoming_request"]
