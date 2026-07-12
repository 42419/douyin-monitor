"""监控核心逻辑：API 请求、单用户检测、状态快照。"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

from .config import (
    DELETE_CONFIRM_ROUNDS,
    DELETE_CONFIRM_ROUNDS_TOP,
    HTTP_TIMEOUT,
    KNOWN_IDS_MAX,
    MAX_CONSECUTIVE_FAILS,
    STALE_FALLBACK_DAYS,
    STALE_HASH_ALERT_COOLDOWN,
    STATE_DIR,
    STATUS_FILE,
    Config,
)
from .notifiers.base import BaseNotifier
from .state import UserState
from .utils import now_iso, now_str, seconds_since


# =================== API 请求 ===================
def fetch_user_videos(api_url: str, sec_user_id: str, count: int) -> Tuple[int, str]:
    """返回 (http_code, raw_text)。http_code == 0 表示网络层异常。"""
    try:
        resp = requests.get(
            api_url,
            params={"sec_user_id": sec_user_id, "count": count},
            headers={"User-Agent": "douyin-monitor-py/1.0"},
            timeout=HTTP_TIMEOUT,
        )
        return resp.status_code, resp.text
    except requests.exceptions.RequestException as e:
        return 0, str(e)


# =================== 监控核心逻辑 ===================
class Monitor:
    def __init__(self, cfg: Config, notifier: BaseNotifier, stop_event: threading.Event):
        self.cfg = cfg
        self.notifier = notifier
        self._stop_event = stop_event

    def _on_fail(self, state: UserState, nickname: str) -> None:
        from .config import FAIL_COOLDOWN

        fails = state.data.get("consecutive_fails", 0) + 1
        state.data["consecutive_fails"] = fails
        logging.warning(f"用户 {nickname} API 连续失败: {fails}/{MAX_CONSECUTIVE_FAILS}")

        if fails >= MAX_CONSECUTIVE_FAILS:
            elapsed = seconds_since(state.data.get("last_fail_alert_at"))
            if elapsed is None or elapsed >= FAIL_COOLDOWN:
                logging.error(f"用户 {nickname} 连续失败 {fails} 次")
                self.notifier.send_text(
                    "抖音监控异常",
                    f"用户 **{nickname}** 连续 **{fails}** 次请求失败\n\n"
                    "可能原因：\n"
                    "- 抖音 Cookie 已过期，需更新\n"
                    "- API 服务未运行或异常\n"
                    "- 网络问题\n\n"
                    f"请尽快检查！\n\n{now_str()}",
                )
                state.data["last_fail_alert_at"] = now_iso()
                state.data["fail_alerted"] = True

    def _on_success(self, state: UserState, nickname: str) -> None:
        fails = state.data.get("consecutive_fails", 0)
        if fails >= MAX_CONSECUTIVE_FAILS and state.data.get("fail_alerted"):
            logging.info(f"用户 {nickname} API 已恢复正常（之前连续失败 {fails} 次）")
            self.notifier.send_text(
                f"用户 {nickname} 监控已恢复",
                f"用户 **{nickname}** 之前连续失败 **{fails}** 次，现已恢复正常。\n\n{now_str()}",
            )
            state.data["fail_alerted"] = False
        state.data["consecutive_fails"] = 0

    def _check_stale(self, state: UserState, nickname: str, aweme_list: Optional[list]) -> None:
        legacy_alert_at = state.data.get("last_stale_alert_at")

        # 检测 1：API 响应哈希连续不变
        if aweme_list is not None:
            ids_sorted = sorted(item.get("aweme_id", "") for item in aweme_list)
            resp_hash = hashlib.sha256("\n".join(ids_sorted).encode("utf-8")).hexdigest()
            prev_hash = state.data.get("resp_hash")

            if prev_hash == resp_hash and prev_hash is not None:
                elapsed = seconds_since(state.data.get("resp_hash_since")) or 0
                if elapsed >= self.cfg.stale_threshold:
                    hours = int(elapsed // 3600)
                    last_alert_elapsed = seconds_since(
                        state.data.get("last_hash_stale_alert_at") or legacy_alert_at
                    )
                    if last_alert_elapsed is None or last_alert_elapsed >= STALE_HASH_ALERT_COOLDOWN:
                        logging.error(
                            f"用户 {nickname} API 响应连续 {hours} 小时无变化，疑似 Cookie 过期"
                        )
                        self.notifier.send_text(
                            "抖音数据可能过时",
                            f"用户 **{nickname}** 的 API 响应已连续 **{hours}** 小时未变化\n\n"
                            "强烈疑似 Cookie 已过期，API 返回缓存旧数据\n\n"
                            "建议：更新 Cookie 后重启监控脚本\n\n"
                            f"{now_str()}",
                        )
                        state.data["last_hash_stale_alert_at"] = now_iso()
            else:
                state.data["resp_hash"] = resp_hash
                state.data["resp_hash_since"] = now_iso()

        # 检测 2：长期无新视频兜底（一次性提醒：达到阈值只提醒一次，
        # 不会每天/每小时反复刷屏；直到用户发布新视频、状态被重置后，
        # 未来再次连续无更新达到阈值时才会重新提醒一次）
        last_update = state.data.get("last_update_at") or state.data.get("initialized_at")
        elapsed_days = seconds_since(last_update)
        if elapsed_days is not None and elapsed_days >= STALE_FALLBACK_DAYS * 86400:
            if not state.data.get("fail_alerted") and not state.data.get("stale_alerted"):
                days = int(elapsed_days // 86400)
                logging.info(f"用户 {nickname} 已 {days} 天无新视频，发送一次性提醒")
                self.notifier.send_text(
                    "长期无更新提醒",
                    f"用户 **{nickname}** 已 **{days}** 天没有发布新视频\n\n"
                    "可能是用户近期未更新，或 Cookie 已过期\n\n"
                    "（这条提醒只会发一次，该用户发布新视频后才会重新计算）\n\n"
                    f"{now_str()}",
                )
                state.data["last_fallback_stale_alert_at"] = now_iso()
                state.data["stale_alerted"] = True

    def check_user(self, sec_user_id: str, nickname: str) -> dict:
        state = UserState(STATE_DIR / f"{sec_user_id}.json")
        result = {"status": "ok", "new_count": 0, "deleted_count": 0}
        try:
            inner_result = self._check_user_inner(state, sec_user_id, nickname)
            if inner_result:
                result = inner_result
        finally:
            state.save()
        return result

    def _check_user_inner(self, state: UserState, sec_user_id: str, nickname: str) -> dict:
        state.data["sec_user_id"] = sec_user_id
        state.data["nickname"] = nickname

        logging.debug(f"检查用户: {nickname}")
        http_code, raw = fetch_user_videos(self.cfg.api_url, sec_user_id, self.cfg.fetch_count)

        if http_code == 0:
            logging.error(f"用户 {nickname} 网络请求异常: {raw}")
            self._on_fail(state, nickname)
            return {"status": "fail", "new_count": 0, "deleted_count": 0}
        if http_code != 200:
            logging.error(f"用户 {nickname} API HTTP 错误: {http_code}")
            self._on_fail(state, nickname)
            return {"status": "fail", "new_count": 0, "deleted_count": 0}
        if not raw:
            logging.error(f"用户 {nickname} API 响应体为空")
            self._on_fail(state, nickname)
            return {"status": "fail", "new_count": 0, "deleted_count": 0}

        try:
            resp = json.loads(raw)
        except json.JSONDecodeError:
            logging.error(f"用户 {nickname} API 响应非合法 JSON")
            self._on_fail(state, nickname)
            return {"status": "fail", "new_count": 0, "deleted_count": 0}

        if resp.get("code") != 200:
            logging.error(f"用户 {nickname} API 返回错误: {resp.get('msg', '未知错误')}")
            self._on_fail(state, nickname)
            return {"status": "fail", "new_count": 0, "deleted_count": 0}

        aweme_list = (resp.get("data") or {}).get("aweme_list") or []
        if not aweme_list:
            logging.warning(f"用户 {nickname} API 返回空列表，可能 Cookie 过期")
            self._on_fail(state, nickname)
            self._check_stale(state, nickname, aweme_list)
            return {"status": "fail", "new_count": 0, "deleted_count": 0}

        self._on_success(state, nickname)

        videos: Dict[str, dict] = state.data.setdefault("videos", {})
        pending_deletes: Dict[str, int] = state.data.setdefault("pending_deletes", {})
        current_map: Dict[str, dict] = {}
        for item in aweme_list:
            vid = item.get("aweme_id")
            if not vid:
                continue
            stats = item.get("statistics") or {}
            video_info = item.get("video") or {}
            cover_info = video_info.get("cover") or {}
            current_map[vid] = {
                "title": item.get("item_title") or item.get("desc") or "无标题",
                "desc": item.get("desc") or "",
                "create_time": int(item.get("create_time") or 0),
                "is_top": bool(item.get("is_top")),
                "cover_url": (cover_info.get("url_list") or [None])[0],
                "digg_count": int(stats.get("digg_count") or 0),
                "comment_count": int(stats.get("comment_count") or 0),
                "share_count": int(stats.get("share_count") or 0),
                "collect_count": int(stats.get("collect_count") or 0),
                "duration_ms": int(item.get("duration") or video_info.get("duration") or 0),
            }

        if aweme_list and not current_map:
            logging.error(f"用户 {nickname} API 返回的视频列表字段异常（缺少 aweme_id），本轮跳过")
            self._on_fail(state, nickname)
            self._check_stale(state, nickname, aweme_list)
            return {"status": "fail", "new_count": 0, "deleted_count": 0}

        current_ids = set(current_map)
        known_ids = set(videos)

        for vid in list(pending_deletes):
            if vid in current_ids:
                pending_deletes.pop(vid, None)

        # 首次记录
        if not videos:
            for vid, meta in current_map.items():
                videos[vid] = {
                    "title": meta["title"],
                    "create_time": meta["create_time"],
                    "is_top": meta["is_top"],
                }
            ts = now_iso()
            state.data["initialized_at"] = ts
            state.data["last_update_at"] = ts
            logging.info(f"首次记录用户 {nickname}，初始化已知视频列表（{len(current_map)} 条）")
            self._check_stale(state, nickname, aweme_list)
            return {"status": "init", "new_count": len(current_map), "deleted_count": 0}

        # 置顶状态变化 & 标题变更检测
        title_changed_count = 0
        for vid in current_ids & known_ids:
            entry = videos[vid]
            old_title = entry.get("title")
            new_title = current_map[vid]["title"]
            if new_title != old_title:
                logging.info(
                    f"用户 {nickname} 视频标题变更 [{vid}]："
                    f"「{old_title or '(无标题)'}」 -> 「{new_title}」"
                )
                entry["title"] = new_title
                title_changed_count += 1
            entry["is_top"] = current_map[vid]["is_top"]

        new_ids = current_ids - known_ids
        disappeared_ids = known_ids - current_ids

        if not new_ids and not disappeared_ids:
            if title_changed_count:
                state.data["last_update_at"] = now_iso()
                logging.debug(f"用户 {nickname} 无新增/删除，但有 {title_changed_count} 条标题变更")
            else:
                logging.debug(f"用户 {nickname} 无更新")
            self._check_stale(state, nickname, aweme_list)
            return {
                "status": "ok",
                "new_count": 0,
                "deleted_count": 0,
                "title_changed_count": title_changed_count,
            }

        content_changed = title_changed_count > 0
        real_deleted_count = 0

        # 删除检测（二次确认）
        scrolled_out_ids: List[str] = []
        confirmed_deleted_ids: List[str] = []

        for vid in disappeared_ids:
            meta = videos.get(vid, {})
            is_top = bool(meta.get("is_top"))

            if not is_top and new_ids:
                scrolled_out_ids.append(vid)
                pending_deletes.pop(vid, None)
                continue

            required = DELETE_CONFIRM_ROUNDS_TOP if is_top else DELETE_CONFIRM_ROUNDS
            count = pending_deletes.get(vid, 0) + 1
            pending_deletes[vid] = count
            if count >= required:
                confirmed_deleted_ids.append(vid)

        if scrolled_out_ids:
            logging.info(
                f"用户 {nickname} 有 {len(scrolled_out_ids)} 条视频被新视频挤出窗口，静默清理"
            )
            for vid in scrolled_out_ids:
                videos.pop(vid, None)

        if confirmed_deleted_ids:
            deleted_entries = [(vid, videos.get(vid, {})) for vid in confirmed_deleted_ids]
            logging.info(f"用户 {nickname} 确认检测到 {len(deleted_entries)} 条视频被删除")
            self.notifier.send_deleted(nickname, deleted_entries, self.cfg.at_mobiles)
            for vid in confirmed_deleted_ids:
                videos.pop(vid, None)
                pending_deletes.pop(vid, None)
            content_changed = True
            real_deleted_count = len(deleted_entries)
            self._stop_event.wait(1)

        still_pending = [
            vid for vid in disappeared_ids
            if vid not in scrolled_out_ids and vid not in confirmed_deleted_ids
        ]
        if still_pending:
            logging.debug(
                f"用户 {nickname} 有 {len(still_pending)} 条视频疑似消失，"
                f"待后续轮次确认（未达到确认阈值，暂不告警/不移除）"
            )

        # 新视频通知
        if new_ids:
            if len(new_ids) >= self.cfg.fetch_count:
                logging.warning(
                    f"用户 {nickname} 本轮新增 {len(new_ids)} 条视频，已达到抓取窗口上限 "
                    f"(FETCH_COUNT={self.cfg.fetch_count})，可能存在漏检，建议调大 FETCH_COUNT"
                )
            # 用户重新发布了新视频，长期无更新的一次性提醒重置，
            # 以后再次连续无更新达到阈值时可以重新提醒一次
            state.data["stale_alerted"] = False

            # 距上次发布新视频的间隔天数（基于这一轮覆盖之前的 last_update_at 计算），
            # 同一轮新增的多条视频共用这个间隔（都是相对"上一次已知更新"而言）
            prev_last_update = state.data.get("last_update_at") or state.data.get("initialized_at")
            gap_seconds = seconds_since(prev_last_update)
            gap_days = int(gap_seconds // 86400) if gap_seconds is not None else None

            new_items = sorted(
                ({**current_map[v], "id": v} for v in new_ids),
                key=lambda x: x["create_time"],
            )
            logging.info(f"用户 {nickname} 检测到 {len(new_items)} 条新视频")
            for item in new_items:
                self.notifier.send_video(
                    nickname,
                    item["id"],
                    item["title"],
                    item["create_time"],
                    item.get("cover_url"),
                    digg_count=item.get("digg_count", 0),
                    comment_count=item.get("comment_count", 0),
                    share_count=item.get("share_count", 0),
                    collect_count=item.get("collect_count", 0),
                    duration_ms=item.get("duration_ms", 0),
                    desc=item.get("desc", ""),
                    gap_days=gap_days,
                )
                videos[item["id"]] = {
                    "title": item["title"],
                    "create_time": item["create_time"],
                    "is_top": item["is_top"],
                }
                self._stop_event.wait(1)
            content_changed = True

        if content_changed:
            state.data["last_update_at"] = now_iso()

        # 裁剪已知视频列表上限
        if len(videos) > KNOWN_IDS_MAX:
            overflow = len(videos) - KNOWN_IDS_MAX
            non_top_entries = [(vid, m) for vid, m in videos.items() if not m.get("is_top")]
            oldest = sorted(non_top_entries, key=lambda kv: kv[1].get("create_time", 0))[:overflow]
            for vid, _ in oldest:
                videos.pop(vid, None)
                pending_deletes.pop(vid, None)

        self._check_stale(state, nickname, aweme_list)

        return {
            "status": "ok",
            "new_count": len(new_ids),
            "deleted_count": real_deleted_count,
            "title_changed_count": title_changed_count,
        }

    def write_status_snapshot(self, users: List[Tuple[str, str]]) -> None:
        current_ids = {sec_user_id for sec_user_id, _ in users}
        entries = []
        if STATE_DIR.exists():
            for path in sorted(STATE_DIR.glob("*.json")):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                sec_user_id = data.get("sec_user_id") or path.stem
                nickname = data.get("nickname") or sec_user_id
                last_update = data.get("last_update_at") or data.get("initialized_at")
                elapsed = seconds_since(last_update)
                age_hours = int(elapsed // 3600) if elapsed is not None else None
                videos = data.get("videos", {})
                entries.append(
                    {
                        "sec_user_id": sec_user_id,
                        "nickname": nickname,
                        "last_update": last_update,
                        "initialized_at": data.get("initialized_at"),
                        "known_videos": len(videos),
                        "hours_since_update": age_hours,
                        "consecutive_fails": data.get("consecutive_fails", 0),
                        "in_users_conf": sec_user_id in current_ids,
                        "update_frequency": _classify_update_frequency(videos),
                    }
                )

        snapshot = {"timestamp": now_str(), "pid": os.getpid(), "users": entries}
        STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATUS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STATUS_FILE)


def _classify_update_frequency(videos: Dict[str, dict]) -> Optional[str]:
    """根据已知视频（排除置顶，因为置顶视频的发布时间不代表更新节奏）之间
    的平均发布间隔，粗略估计这个账号的更新频率，用于 Web 面板展示。
    已知视频不足 2 条时没法算间隔，返回 None（面板上不显示频率标签）。
    """
    create_times = sorted(
        meta.get("create_time", 0)
        for meta in videos.values()
        if not meta.get("is_top") and meta.get("create_time")
    )
    if len(create_times) < 2:
        return None

    gaps = [b - a for a, b in zip(create_times, create_times[1:]) if b > a]
    if not gaps:
        return None
    avg_days = (sum(gaps) / len(gaps)) / 86400

    if avg_days <= 1.5:
        return "日更"
    if avg_days <= 4:
        return "隔天更新"
    if avg_days <= 10:
        return "周更"
    if avg_days <= 20:
        return "半月更"
    if avg_days <= 45:
        return "月更"
    return "更新较少"
