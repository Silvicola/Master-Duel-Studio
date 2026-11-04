"""bundle 文件名 = crc32(逻辑路径)"""
import zlib

_STRIP = ("#/", "<_PLATFORM_>/", "<_RESOURCE_TYPE_>", "<_CARD_ILLUST_>")


def get_crc(path: str) -> str:
    # 注意: 不能 lower(), 大小写敏感. 对应游戏里 ResourceUtility.GetCrc
    for tok in _STRIP:
        path = path.replace(tok, "")
    return format(zlib.crc32(path.encode("utf-8")) & 0xFFFFFFFF, "08x")


def resolve_illust(path: str, region: str = "tcg") -> str:
    # 坑: runfx 的 effectPath 里 <_CARD_ILLUST_> 是被替换成 tcg 再算 crc, 跟 get_crc 的删掉不一样
    return path.replace("<_CARD_ILLUST_>", region)
