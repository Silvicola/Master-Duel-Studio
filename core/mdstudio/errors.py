class MDStudioError(Exception):
    """所有给用户看的错误都走这个, CLI/GUI 只打印 message"""


class GameNotFound(MDStudioError):
    pass


class GameRunning(MDStudioError):
    pass


class AssetMissing(MDStudioError):
    pass


class AssetInterrupted(MDStudioError):
    """下载中途断了。已下的残片还留着，用户可以再点一次接着下。
    单独一类是为了让界面知道该不该说「再点一次就能续」，不靠匹配文案。"""


class NotOurs(MDStudioError):
    """卡不是我们加的 (官方自带或别的工具), 不敢动"""


class CardNotFound(MDStudioError):
    pass


class Ambiguous(MDStudioError):
    """名字对上好几张 (或者同名不同 id). 不替用户猜, 交给调用方让用户选"""

    def __init__(self, query, candidates):
        self.query = query
        self.candidates = candidates      # [id, ...]
        from mdstudio.i18n import tr
        super().__init__(tr('"%s" matches %s cards, pick one by ID') % (query, len(candidates)))
