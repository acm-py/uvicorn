# wrk -d20s -t10 -c200 http://127.0.0.1:8080/
# gunicorn app:hello_world --bind localhost:8080 --worker-class worker.ASGIWorker
# https://github.com/MagicStack/httptools - Fast HTTP parsing.
# https://github.com/aio-libs/aiohttp - An asyncio framework, including gunicorn worker.
# https://github.com/channelcat/sanic - An asyncio framework, including gunicorn worker.
# https://python-hyper.org/projects/h2/en/stable/asyncio-example.html - HTTP2 parser
# https://github.com/jeamland/guvnor - Gunicorn worker implementation
import asyncio
import functools
import io
import os

import httptools
import uvloop

from gunicorn.workers.base import Worker


class HttpProtocol(asyncio.Protocol):
    """sumary_line
    asyncio.Protocol 是定义异步网络通信协议的接口类
    继承后可以自定义网络协议来处理数据的收发，并在异步的事件循环中进行处理。
    通过继承这个类可以构建各种类型的异步网络应用（tcp、udp、http等）
    Keyword arguments:
    argument -- description
    Return: return_description
    """

    def __init__(self, consumer, loop, sock, cfg):
        """sumary_line

        Keyword arguments:
        consumer -- 消费者对象
        loop -- 事件循环
        sock -- 套接字对象
        cfg -- 配置对象
        Return: None
        """
        # 创建一个httpRequestparser 对象，并将实例（self）作为参数传递给它。
        self.request_parser = httptools.HttpRequestParser(self)
        self.consumer = consumer
        self.loop = loop
        self.transport = None

        self.base_message = {
            "reply_channel": self,
            "scheme": "https" if cfg.is_ssl else "http",
            "root_path": os.environ.get("SCRIPT_NAME", ""),
            "server": sock.getsockname(),
        }

    # The asyncio.Protocol hooks...
    # 底层的传输对象，提供了发送和接受数据的方法
    def connection_made(self, transport):
        self.transport = transport

    # 当连接丢失时调用
    def connection_lost(self, exc):
        self.transport = None

    # 接受结束时
    def eof_received(self):
        pass

    # 当收到新的数据时，调用
    def data_received(self, data):
        self.request_parser.feed_data(data)

    # Event hooks called back into by HttpRequestParser...
    # 下面是由HttpRequestParser调用的钩子
    def on_message_begin(self):
        """开始接受消息时回调"""
        self.message = self.base_message.copy()
        self.headers = []
        self.body = []

    def on_url(self, url):
        """
        收到url时回调
        """
        parsed = httptools.parse_url(url)
        method = self.request_parser.get_method()
        http_version = self.request_parser.get_http_version()

        self.message.update(
            {
                "http_version": http_version,
                "method": method.decode("ascii"),
                "path": parsed.path.decode("ascii"),
                "query_string": parsed.query if parsed.query else b"",
            }
        )

    def on_header(self, name: bytes, value: bytes):
        """接受请求头时回调"""
        self.headers.append([name.lower(), value])

    def on_body(self, body: bytes):
        self.body.append(body)

    def on_message_complete(self):
        """消息完整时回调"""
        self.message["headers"] = self.headers
        self.message["body"] = b"".join(self.body)
        self.consumer(
            {"reply_channel": self, "channel_layer": self, "message": self.message}
        )

    def on_chunk_header(self):
        pass

    def on_chunk_complete(self):
        pass

    # Called back into by the ASGI consumer...
    def send(self, message):
        """构建HTTP响应"""
        if self.transport is None:
            return

        status_bytes = str(message["status"]).encode()
        content = message.get("content")

        response = [b"HTTP/1.1 ", status_bytes, b"\r\n"]
        for header_name, header_value in message["headers"]:
            response.extend([header_name, b": ", header_value, b"\r\n"])
        response.append(b"\r\n")

        self.transport.write(b"".join(response))
        if content:
            self.transport.write(content)

        if not self.request_parser.should_keep_alive():
            self.transport.close()


class UvicornWorker(Worker):
    """
    A worker class for Gunicorn that interfaces with an ASGI consumer callable,
    rather than a WSGI callable.

    We use a couple of packages from MagicStack in order to achieve an
    extremely high-throughput and low-latency implementation:

    * `uvloop` as the event loop policy.
    * `httptools` as the HTTP request parser.
    """

    def init_process(self):
        # Close any existing event loop before setting a
        # new policy.
        # 先关闭任何现有的事件循环
        asyncio.get_event_loop().close()

        # Setup uvloop policy, so that every
        # asyncio.get_event_loop() will create an instance
        # of uvloop event loop.
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

        super().init_process()

    def run(self):
        loop = asyncio.get_event_loop()
        loop.create_task(self.create_servers(loop))
        loop.run_forever()

    async def create_servers(self, loop):
        cfg = self.cfg
        consumer = self.wsgi

        # 为每一个套接字创建一个服务器，并传递给 `HttpProtocol`类的实例化对象
        for sock in self.sockets:
            protocol = functools.partial(
                HttpProtocol, consumer=consumer, loop=loop, sock=sock, cfg=cfg
            )
            await loop.create_server(protocol, sock=sock)
