"""共享的不透明 id 生成。"""
import secrets


def new_id():
    """24 位十六进制，用于 chatcmpl-/msg-/resp- 这类对外 id。

    两个代理以前一个是 secrets.token_hex(12)、一个是 md5(os.urandom(8))[:24]，
    形状一样、内容都是随机，所以统一成一种不会影响任何调用方。
    """
    return secrets.token_hex(12)
