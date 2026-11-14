#!/usr/bin/env python3
"""飞书取图链路：用**假 client** 跑真代码，不联网。

为什么要有这个测试（而不是只测内核）：
    2026-11-14 真机翻车：取图方法里用了 `time.time()`，但模块级**没有 import time**
    ⇒ `NameError`；而它被 `_inbound_worker` 的外层 `except` 吞掉，消息**既不处理也
    不回复**，用户侧表现是"发图片完全没动静"。只测内核是发现不了的 ——
    **必须真的调那个方法**。

覆盖两层：
    · 平台层 `_feishu_fetch_image()`：成功 / 平台拒绝 / 空响应 / SDK 异常 / 缺字段 /
      取回来的不是图片；
    · 内核 `_image_local_prompt()`：按魔数认格式并改名（`.bin` 会被读图工具拒），
      以及"文件不在 / 不是图片"两条失败路径。

用法：`python -X utf8 tools/test_feishu_fetch_image.py`
"""

import io
import os
import struct
import sys
import tempfile
import zlib
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

from core.bridge_core import CoreMixin                      # noqa: E402
from platforms.feishu.codec import FeishuCodecMixin         # noqa: E402

OK = True


def check(name, cond, extra=""):
    global OK
    OK = OK and bool(cond)
    print(("  ✅ " if cond else "  ❌ ") + name + (("  " + extra) if extra else ""))


def make_png(w=32, h=32):
    rows = [bytes([0]) + bytes((10, 20, 30)) * w for _ in range(h)]
    raw = b"".join(rows)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


class FakeResp:
    """仿真 `GetMessageResourceResponse`：`success()` / `code` / `msg` / `file`。"""

    def __init__(self, payload=None, code=0, msg="ok"):
        self.code = code
        self.msg = msg
        self.file = io.BytesIO(payload) if payload is not None else None

    def success(self):
        return self.code == 0


class FakeResource:
    def __init__(self, resp=None, raises=None):
        self._resp, self._raises = resp, raises

    def get(self, req):
        if self._raises:
            raise self._raises
        return self._resp


class FakeCli:
    """仿真 `lark_oapi.Client` 的最小路径：`cli.im.v1.message_resource`。"""

    def __init__(self, resource):
        class V1:
            pass
        self.im = type("Im", (), {})()
        self.im.v1 = V1()
        self.im.v1.message_resource = resource


def fetch(inc, resp=None, raises=None):
    obj = FeishuCodecMixin.__new__(FeishuCodecMixin)
    obj.cli = FakeCli(FakeResource(resp, raises))
    obj._feishu_fetch_image(inc)
    return inc


def test_normalize():
    """适配层必须**真的**从 `content` 里解出 image_key。

    2026-11-15 真机：发图后桥回"回调里没有可下载的链接（字段：无）"——
    那是旧内核在 image_url 为空时的老提示，而适配层当时根本还没解 `image_key`。
    这条测试钉住"解析这一步"，否则整条链路第一步就是空的。
    """
    print("\n== 适配层：从 content 里解出 image_key ==")
    obj = FeishuCodecMixin.__new__(FeishuCodecMixin)
    obj._shape_logged = True                    # 跳过首次形状日志
    m = SimpleNamespace(chat_id="oc_1", chat_type="group", message_type="image",
                        message_id="om_1",
                        mentions=[SimpleNamespace(key="@_user_1", name="CC")],
                        content='{"image_key":"img_v3_abc"}')
    s = SimpleNamespace(sender_type="user",
                        sender_id=SimpleNamespace(open_id="ou_1"))
    inc = obj._feishu_normalize(SimpleNamespace(message=m, sender=s))
    check("msgtype 认成 image", inc.get("msgtype") == "image", str(inc.get("msgtype")))
    check("image_key 解出来了", inc.get("image_key") == "img_v3_abc",
          str(inc.get("image_key")))
    check("image_keys 记录了实际出现的字段名",
          inc.get("image_keys") == ["image_key"], str(inc.get("image_keys")))
    check("飞书图片没有 image_url（字节只能平台层代取）",
          not inc.get("image_url"))

    m2 = SimpleNamespace(chat_id="oc_1", chat_type="group", message_type="image",
                         message_id="om_2",
                         mentions=[SimpleNamespace(key="@_user_1", name="CC")],
                         content="不是 JSON")
    inc2 = obj._feishu_normalize(SimpleNamespace(message=m2, sender=s))
    check("坏 content：不炸，key 为空、字段留空", inc2.get("image_key") == ""
          and inc2.get("image_keys") == [], str(inc2.get("image_keys")))


def test_platform():
    print("\n== 平台层：把字节取下来落盘 ==")
    png = make_png()

    inc = fetch({"msgtype": "image", "image_key": "img_v3_abc", "dedupe_key": "om_1"},
                FakeResp(png))
    path = inc.get("image_local") or ""
    check("成功取图：写入了 image_local", bool(path), path)
    check("成功取图：文件真的存在", bool(path) and os.path.exists(path))
    check("成功取图：大小对得上", inc.get("image_total") == len(png),
          str(inc.get("image_total")))
    check("成功取图：一律存 .bin（格式交给内核判）", path.endswith(".bin"), path)
    check("成功取图：没有 image_error", not inc.get("image_error"),
          str(inc.get("image_error")))
    if path and os.path.exists(path):
        with open(path, "rb") as f:
            check("成功取图：内容逐字节一致", f.read() == png)

    inc = fetch({"msgtype": "image", "image_key": "k", "dedupe_key": "m"},
                FakeResp(None, code=234003, msg="file not exist"))
    check("平台拒绝：记了 image_error（原样带 code/msg）",
          "下载被拒" in (inc.get("image_error") or ""), str(inc.get("image_error")))
    check("平台拒绝：没有 image_local", not inc.get("image_local"))

    inc = fetch({"msgtype": "image", "image_key": "k", "dedupe_key": "m"}, FakeResp(b""))
    check("空响应体：报 0 字节", "空的" in (inc.get("image_error") or ""),
          str(inc.get("image_error")))

    inc = fetch({"msgtype": "image", "image_key": "k", "dedupe_key": "m"},
                raises=RuntimeError("network down"))
    check("SDK 抛异常：收成 image_error（不往上抛）",
          "network down" in (inc.get("image_error") or ""), str(inc.get("image_error")))

    inc = fetch({"msgtype": "image", "image_key": "", "dedupe_key": "m"})
    check("缺字段：报缺 image_key", "缺 image_key" in (inc.get("image_error") or ""),
          str(inc.get("image_error")))

    inc = fetch({"msgtype": "image", "image_key": "k", "dedupe_key": "m"},
                FakeResp(b"<html>403</html>"))
    check("取回来的不是图片：这一层不判格式（交给内核）", bool(inc.get("image_local")))


def test_kernel():
    print("\n== 内核：按魔数认格式、改成正确扩展名 ==")
    core = CoreMixin.__new__(CoreMixin)

    d = tempfile.mkdtemp(prefix="img-bin-")
    bin_path = os.path.join(d, "220000-deadbeef.bin")
    with open(bin_path, "wb") as f:
        f.write(make_png())

    prompt, why = core._image_local_prompt({"image_local": bin_path}, "帮我看看这张图")
    check("能出提示", bool(prompt) and not why, why)
    names = os.listdir(d)
    check(".bin 被改成 .png", names == ["220000-deadbeef.png"], str(names))
    check("提示里写的是改名后的路径",
          "220000-deadbeef.png" in prompt and ".bin" not in prompt)
    check("提示里带上了用户附言", "帮我看看这张图" in prompt)
    check("提示**不承诺**一定能看清", "我看不到图片内容" in prompt)
    check("提示没写死单一 harness 的工具名",
          "`Read`" in prompt and "read_image" in prompt)

    prompt2, why2 = core._image_local_prompt({"image_local": ""})
    check("没有 image_local：返回原因而不是提示", not prompt2 and bool(why2), why2)

    bad = os.path.join(tempfile.mkdtemp(prefix="img-bad-"), "x.bin")
    with open(bad, "wb") as f:
        f.write(b"<html>403</html>")
    prompt3, why3 = core._image_local_prompt({"image_local": bad})
    check("不是图片：返回原因", not prompt3 and "不像图片" in (why3 or ""), why3)


def main():
    test_normalize()
    test_platform()
    test_kernel()
    print("\n" + ("✅ 全部通过" if OK else "❌ 有失败项"))
    return 0 if OK else 2


if __name__ == "__main__":
    sys.exit(main())