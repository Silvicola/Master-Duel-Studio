class MDStudioError(Exception):
    """所有给用户看的错误都走这个, CLI/GUI 只打印 message"""


class GameNotFound(MDStudioError):
    pass


class GameRunning(MDStudioError):
    pass


class AssetMissing(MDStudioError):
    pass


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
