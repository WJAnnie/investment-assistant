from .akshare import AkShareProvider


class EastMoneyProvider(AkShareProvider):
    """东方财富数据源适配器，通过 AkShare 的公开包装接口访问。"""
