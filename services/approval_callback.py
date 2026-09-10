"""审批 callback 服务 —— 独立进程（CLAUDE.md 铁律 2）。

这个进程是**唯一**能写 decisions 表的身份。与 Agent 同进程时列级 GRANT 形同虚设。

ponytail: 用 stdlib http.server。需求是「接住邮件里的一次点击」，
不值得为此引入 Flask/FastAPI 与 ASGI 栈。

跑法：
    DATASTEWARD_TOKEN_SECRET=... python3 services/approval_callback.py
"""
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json

import notify
import tokens
from plugins.datasteward_gate.approvals import Store, open_store, rows_of

DB = os.environ.get("DATASTEWARD_DB", os.path.expanduser("~/.datalaker/approvals.db"))
PORT = int(os.environ.get("APPROVAL_PORT", "8787"))
REQUIRE_CONFIRM = os.environ.get("REQUIRE_DOUBLE_CONFIRM", "1") not in ("0", "false", "")

PAGE = """<!doctype html><meta charset=utf-8>
<title>{title}</title>
<style>body{{font:16px/1.6 -apple-system,system-ui,sans-serif;max-width:34rem;
margin:4rem auto;padding:0 1.5rem;color:#1a1a1a}}
.b{{display:inline-block;padding:.3rem .7rem;border-radius:.3rem;font-weight:600;
font-size:.85rem;background:{bg};color:{fg}}}
.m{{color:#666;font-size:.9rem;margin-top:1.5rem}}</style>
<p><span class=b>{badge}</span></p><h2>{title}</h2><p>{body}</p>
<p class=m>{note}</p>"""


def page(title, body, badge, bg, fg, note=""):
    return PAGE.format(title=title, body=body, badge=badge, bg=bg, fg=fg, note=note)


class Handler(BaseHTTPRequestHandler):
    server_version = "DataStewardApproval/0.1"

    def log_message(self, fmt, *args):          # 静音默认访问日志
        pass

    def _send(self, code, html):
        raw = html.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _json(self, code, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/health":
            # **报出自己是谁在应答。** 只回 "ok" 的话，端口上残留的
            # 上一次实例也能让探活通过 —— 而它拿的是另一个库，
            # 于是令牌全对不上，整组测试红在「点击批准失败」，
            # 看起来像审批链路坏了。实测撞过一次，查了半天是残留进程。
            import os as _os
            return self._send(200, f"ok pid={_os.getpid()} db={DB}")
        # 资产台账的只读接口 —— 审计、BI、别的系统从这里拉血缘。
        # **只读、无副作用**，所以不需要令牌；但它也只吐台账里的东西，
        # 口令从来不进台账（见 `_record_provenance` 的 `_SECRET_ARGS`）。
        if u.path == "/provenance":
            qs = parse_qs(u.query)
            asset = (qs.get("asset") or [None])[0]
            try:
                limit = min(int((qs.get("limit") or ["500"])[0]), 5000)
            except ValueError:
                limit = 500
            try:
                with open_store(readonly=False, init_schema=True) as store:
                    rows = store.provenance(asset, limit=limit)
            except Exception as e:                            # noqa: BLE001
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})
            return self._json(200, {"asset": asset, "count": len(rows),
                                    "records": rows})

        if u.path not in ("/approve", "/deny", "/choose"):
            return self._send(404, page("找不到页面", "链接无效。", "404",
                                        "#eee", "#666"))

        token = (parse_qs(u.query).get("t") or [""])[0]
        if not token:
            return self._send(400, page("缺少令牌", "这个链接不完整。", "错误",
                                        "#fde8e8", "#b42318"))

        # 1. 校验签名与有效期 —— 失败一律 fail closed
        try:
            payload = tokens.verify(token)
        except ValueError as e:
            return self._send(403, page("令牌无效", f"{e}。请向发起人索取新的审批链接。",
                                        "拒绝", "#fde8e8", "#b42318"))

        # 2. 令牌里的意图必须与访问的路径一致，防止改 URL 把 deny 变 approve。
        #    /choose 是三选一（阶段提案）：意图必须是**某个已知选项**，
        #    否则改 URL 就能凭空造出一个决定。
        if u.path == "/choose":
            if payload["d"] not in _stage_keys():
                return self._send(403, page(
                    "令牌与操作不符", "这枚令牌不是用于阶段选择的。", "拒绝",
                    "#fde8e8", "#b42318"))
        else:
            want = "approve" if u.path == "/approve" else "deny"
            if payload["d"] != want:
                return self._send(403, page("令牌与操作不符",
                                            "这枚令牌不是用于该操作的。", "拒绝",
                                            "#fde8e8", "#b42318"))

        # 3. 双重确认（readme 10.5）：第一次点击不落库，只发确认信。
        #    链接被转发多少次都无所谓 —— 确认信只到 approver 的注册邮箱。
        if REQUIRE_CONFIRM and payload.get("st") == "click":
            if not _send_confirm(payload):
                # 发不出确认信 ≠ 服务崩溃。决定没落库，动作依然不会执行；
                # 但必须给点击的人一个明确交代，而不是把连接掐掉。
                return self._send(503, page(
                    "确认邮件发送失败",
                    "决定尚未生效，动作也不会执行。请稍后重试，"
                    "或直接回信告知处理人。",
                    "未生效", "#fde8e8", "#b42318",
                    "通知通道异常不会让门禁放行 —— 这是设计如此。"))
            return self._send(200, page(
                "已发出确认邮件",
                "为防止链接被转发后被他人误点，我们向你的注册邮箱发了一封确认信。"
                "请在那封信里再点一次，决定才会生效。",
                "待确认", "#fef3c7", "#92400e",
                "这一步不依赖你是谁——确认信只会送到发起审批时登记的地址。"))

        # 4. 写入决定。token_jti 唯一约束负责挡住重放。
        #    连接用完即关 —— 否则会持有 SQLite 写锁，把 Agent 进程挡在门外。
        # 三选一落的是 `answered` + `chosen`，不是 approve/deny。
        # `decisions.chosen` 这一列建了很久却一直没人写 —— 判分的
        # silver_gated 读的就是它。
        _is_choice = u.path == "/choose"
        with open_store(readonly=False, init_schema=False) as store:
            ok = store.decide(
                payload["aid"],
                "answered" if _is_choice else payload["d"],
                payload["who"],
                token_jti=payload["jti"],
                client_ip=self.client_address[0],
                user_agent=self.headers.get("User-Agent", ""),
                chosen=payload["d"] if _is_choice else None,
            )
            if not ok:
                return self._send(409, page("这枚链接已经用过了",
                                            "该审批已有决定，重复点击不会改变结果。",
                                            "已处理", "#fef3c7", "#92400e"))
            store.append_event(payload["aid"], f"DECIDED_{payload['d'].upper()}",
                               payload["who"])
            _release_notice(store, payload)
            _receipt(payload)
        if _is_choice:
            _label = {o["key"]: o["label"] for o in _stage_options()}.get(
                payload["d"], payload["d"])
            return self._send(200, page(
                "已记录你的选择", f"你选了：{_label}", "已拍板",
                "#dcfce7", "#166534",
                f"{payload['who']} · 决定已写入审计表，Agent 改不了。"))
        if payload["d"] == "approve":
            return self._send(200, page(
                "已批准", "任务将在下次唤醒时继续执行。", "已批准",
                "#dcfce7", "#166534",
                f"审批人 {payload['who']} · 记录已写入审计表，无法被 Agent 修改。"))
        return self._send(200, page(
            "已拒绝", "该动作不会执行，Agent 也不会就同一动作重复发起审批。",
            "已拒绝", "#fde8e8", "#b42318",
            f"审批人 {payload['who']} · 如需改变，请让 Agent 提出新的方案。"))


def _stage_options():
    """三个选项在 `policy.py`（一处定义）。拿不到就当没有 —— 于是
    `/choose` 的意图校验必然失败，fail closed。"""
    try:
        from plugins.datasteward_gate.policy import STAGE_OPTIONS
        return STAGE_OPTIONS
    except Exception:                                        # noqa: BLE001
        return []


def _stage_keys():
    return {o["key"] for o in _stage_options()}


def _send_confirm(payload) -> bool:
    """发确认信：内含 stage=confirm 的第二枚令牌。

    **通道异常必须被吃掉。** 原先这里的异常会冒到 handler 外，
    Python 的 HTTPServer 直接断开连接（客户端看到 RemoteDisconnected），
    整个服务看起来像挂了——而真正的问题只是缺一个邮件依赖。

    返回 False 表示没发出去。决定不落库，动作照样不会执行。
    """
    import notify
    base = os.environ.get("APPROVAL_BASE_URL", f"http://127.0.0.1:{PORT}").rstrip("/")
    t2 = tokens.issue(payload["aid"], payload["d"], payload["who"], stage="confirm")
    # 三选一走 /choose —— 路径跟着令牌里的意图走，写死 approve/deny
    # 会让确认信里的链接永远验不过（意图与路径不符）。
    if payload["d"] in _stage_keys():
        path = "choose"
        verb = "选择"
    else:
        path = "approve" if payload["d"] == "approve" else "deny"
        verb = "批准" if payload["d"] == "approve" else "拒绝"
    try:
        # `hold=False`：确认信是**人自己刚点的那一下**的回声。走手动模式那道闸
        # 的话，人点了批准反而被自己的闸门扣住，链路直接死在这里。
        notify.get(hold=False).send_notice(
            payload["who"], f"[数据管家] 请确认你的{verb}操作",
            f"有人点击了{verb}链接。若确实是你本人操作，请点击下面的链接确认：\n\n"
            f"{base}/{path}?t={t2}\n\n"
            f"若不是你操作的，忽略本邮件即可——该动作不会执行。")
        return True
    except Exception as e:                                    # noqa: BLE001
        try:
            with open_store(readonly=False, init_schema=False) as st:
                st.append_event(payload["aid"], "CONFIRM_MAIL_FAILED", str(e)[:180])
        except Exception:                                     # noqa: BLE001
            pass
        return False


def _release_notice(store, payload):
    """手动模式下被扣住的那封信：**批了才真发，由这个进程发。**

    Agent 侧只能把信写进 approvals 等着（`notify.HoldNotifier`）；
    真正送出去的动作放在这里，与「Agent 不能批准自己」同一条线 ——
    放行的判断和发出的动作都在人这一侧的进程里。

    拒绝就什么也不发。信留在库里，是一条查得到的记录，不是丢失。
    """
    try:
        rows = rows_of(store, "SELECT tool_name, args_json FROM approvals"
                              " WHERE id = {0}", (payload["aid"],))
        if not rows or rows[0][0] != "send_notice":
            return
        letter = json.loads(rows[0][1] or "{}")
    except Exception as e:                                    # noqa: BLE001
        try:
            store.append_event(payload["aid"], "NOTICE_UNREADABLE", str(e)[:180])
        except Exception:                                     # noqa: BLE001
            pass
        return
    if payload["d"] != "approve":
        store.append_event(payload["aid"], "NOTICE_DROPPED",
                           f'{letter.get("to", "")} · {letter.get("subject", "")}'[:180])
        return
    try:
        # 同样 `hold=False`：这封信已经被人看过并放行了，不能再扣一次。
        # `run_id` 是 HoldNotifier 扣信时一起存进 letter 的。带着它发，
        # 这封放行后的信才和原来那条线是同一个 thread token ——
        # 否则人回它的时候归属又回到靠猜。
        notify.get(hold=False).send_notice(letter.get("to", ""),
                                           letter.get("subject", ""),
                                           letter.get("body", ""),
                                           run_id=letter.get("run_id", ""))
        store.append_event(payload["aid"], "NOTICE_SENT", letter.get("to", "")[:180])
    except Exception as e:                                    # noqa: BLE001
        # **发不出去要留痕。** 人已经点了批准，若这里静默失败，
        # 双方都以为信发出去了 —— 本项目第 1 号坑的又一次变形。
        store.append_event(payload["aid"], "NOTICE_SEND_FAILED", str(e)[:180])


def _receipt(payload):
    """回执：告诉他刚才批准了什么（readme 10.6）。

    在后台线程发送——回执失败不能影响决定落库，决定已经生效了。
    这与 Agent 侧「通知失败 ≠ 门禁打开」是同一条原则的两面。

    **线程自己建连接。** 原先它借调用方那个 store，而调用方是
    `with open_store(...)` —— with 块一退出连接就关了，线程随后拿它查库，
    报「SQLite objects created in a thread can only be used in that same
    thread」。两条路径全崩：回执发不出，连「回执失败」这条事件也写不进去。
    `plugins/datasteward_gate/__init__.py` 的 `_notify_async` 早就写过
    同一条注释（「后台线程必须建自己的连接」），当时只修了那一处。
    """
    import threading

    def _go():
        st = None
        try:
            import notify
            st = open_store(readonly=False, init_schema=False)
            rows = rows_of(st, "SELECT tool_name, args_json FROM approvals "
                                "WHERE id = {0}", (payload["aid"],))
            row = rows[0] if rows else None
            tool = row[0] if row else "(未知动作)"
            target = ""
            if row:
                try:
                    a = json.loads(row[1])
                    target = a.get("table") or a.get("source") or row[1]
                except Exception:
                    target = row[1]
            notify.get(hold=False).send_receipt(
                payload["who"], payload["aid"], payload["d"], tool, target,
                payload["who"])
            st.append_event(payload["aid"], "RECEIPT_SENT", payload["who"])
        except Exception as e:                               # noqa: BLE001
            try:
                (st or open_store(readonly=False, init_schema=False)).append_event(
                    payload["aid"], "RECEIPT_FAILED", str(e)[:180])
            except Exception:                                # noqa: BLE001
                pass          # 连「失败」都记不下来时也不能把线程带崩
        finally:
            try:
                if st is not None:
                    st.close()
            except Exception:                                # noqa: BLE001
                pass

    threading.Thread(target=_go, daemon=True).start()


if __name__ == "__main__":
    if not os.environ.get("DATASTEWARD_DSN"):
        os.makedirs(os.path.dirname(DB), exist_ok=True)
        Store(DB, readonly=False)               # SQLite 模式下确保表已建
    bind_host = os.environ.get("APPROVAL_BIND_HOST", "127.0.0.1")
    print(f"审批 callback 服务 → http://{bind_host}:{PORT}  (db={DB})")
    ThreadingHTTPServer((bind_host, PORT), Handler).serve_forever()
