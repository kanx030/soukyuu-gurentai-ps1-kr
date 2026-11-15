# -*- coding: utf-8 -*-
# 창궁홍련대 한글 패처 (Soukyuu Gurentai PS1 Korean patcher)
# Copyright (C) 2026 창궁홍련대 PS1 한글화 프로젝트
#
# This program is free software; you can redistribute it and/or modify it under the terms of the
# GNU General Public License as published by the Free Software Foundation; either version 2 of the
# License, or (at your option) any later version. This program is distributed WITHOUT ANY WARRANTY;
# see the GNU General Public License (GPL-2.0.txt) for details.
#
# 함께 쓰는 외부 프로그램 (각자의 라이선스):
#   xdelta 3.1.0  (Joshua MacDonald, GPL-2.0)  https://github.com/jmacd/xdelta
#   chdman 0.215  (MAMEdev, GPL-2.0-or-later)  https://github.com/mamedev/mame
#   chdman.dll 래퍼 (선택, RomForge v2.0.4, sinjunyoung, GPL-2.0)  https://github.com/sinjunyoung/RomForge
#     — 래퍼 함수 형식과 호출 방식(작업 폴더 변경, \\?\ 경로, UTF-8)은 RomForge 의
#       CHD/src/tools/chdman_wrapper.h, CHD/Core/Services/ChdmanService.cs 를 참고했다.
"""창궁홍련대 한글 패처 - 핵심 처리 (GUI 와 독립, 명령줄 시험용으로도 사용).

패치 목록은 코드가 아니라 패처 옆 patch\\versions.json 에 있다.
  images : 알려진 이미지(원판·재발매판·한글판 각 버전)의 크기·CRC32·SHA-256 (+CHD SHA1)
  patches: xdelta 파일 하나 = from 이미지 → to 이미지
새 버전이 나오면 manifest 에 이미지와 패치(예: 한글판 v1.0 → v1.1)만 추가하면 된다.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zlib
import ctypes
from collections import deque

SECTOR = 2352
CHUNK = 4 * 1024 * 1024
DEFAULT_NAME = "Soukyuu Gurentai (Korean)"


class Cancelled(Exception):
    pass


class PatchError(Exception):
    pass


class ChdFailed(PatchError):
    """BIN/CUE 결과는 확인까지 끝났는데 CHD 압축만 실패했을 때. files = 남겨 둔 BIN/CUE."""

    def __init__(self, msg, files):
        super().__init__(msg)
        self.files = files


class AlreadyLatest(Exception):
    """입력이 이미 최신 한글판일 때 (오류가 아니라 안내)."""


# ---------------------------------------------------------------- 경로
def app_dir():
    """exe(또는 스크립트)가 있는 폴더."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def tool_path(name):
    """xdelta.exe / chdman.exe 위치 (exe 내장 → 옆 tools 폴더 → 옆)."""
    cands = []
    if getattr(sys, "_MEIPASS", None):
        cands.append(os.path.join(sys._MEIPASS, "tools", name))
    cands.append(os.path.join(app_dir(), "tools", name))
    cands.append(os.path.join(app_dir(), name))
    for c in cands:
        if os.path.isfile(c):
            return c
    raise PatchError(f"도구 파일 {name} 을(를) 찾을 수 없습니다.\n"
                     "패처 exe 옆의 tools 폴더에 xdelta.exe 와 chdman.exe 가 있어야 합니다.\n"
                     "(백신이 지웠다면 압축 파일에서 다시 풀어 주세요.)")


# ---------------------------------------------------------------- manifest
def vkey(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v)))


class Manifest:
    def __init__(self, patch_dir=None):
        self.dir = patch_dir or os.path.join(app_dir(), "patch")
        path = os.path.join(self.dir, "versions.json")
        if not os.path.isfile(path):
            raise PatchError(f"패치 목록 파일이 없습니다:\n{path}\n\n"
                             "패처 exe 옆의 patch 폴더에 versions.json 과 패치 파일이 있어야 합니다.")
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            self.images = {}
            for im in data["images"]:
                im = dict(im)
                im["crc32"] = im["crc32"].upper()
                im["sha256"] = im["sha256"].upper()
                if im.get("md5"):
                    im["md5"] = im["md5"].upper()
                im["chd_sha1"] = [s.lower() for s in im.get("chd_sha1", [])]
                self.images[im["id"]] = im
            self.patches = [dict(p) for p in data["patches"]]
            for p in self.patches:
                if p["from"] not in self.images or p["to"] not in self.images:
                    raise KeyError(p["file"])
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            raise PatchError(f"패치 목록 파일(versions.json)이 올바르지 않습니다:\n{e}")
        kos = [i for i in self.images.values() if i["kind"] == "korean"]
        if not kos:
            raise PatchError("패치 목록에 한글판 정보가 없습니다.")
        self.latest = max(kos, key=lambda i: vkey(i["version"]))

    # -- 조회
    def originals(self):
        return [i for i in self.images.values() if i["kind"] == "original"]

    def koreans(self):
        return sorted((i for i in self.images.values() if i["kind"] == "korean"),
                      key=lambda i: vkey(i["version"]))

    def sizes(self):
        return {i["size"] for i in self.images.values()}

    def by_size(self, size):
        return [i for i in self.images.values() if i["size"] == size]

    def by_chd_sha1(self, sha1s):
        sha1s = {s.lower() for s in sha1s if s}
        return [i for i in self.images.values() if sha1s & set(i["chd_sha1"])]

    def identify(self, info):
        for im in self.images.values():
            if info["size"] == im["size"] and info["crc32"] == im["crc32"] and info["sha256"] == im["sha256"]:
                return im
        return None

    def max_size(self):
        return max(i["size"] for i in self.images.values())

    def find_path(self, start_id):
        """start → 최신 한글판 으로 가는 패치 목록. 단계가 가장 적은 길(직접 패치 우선),
        같으면 패치 파일 합계가 작은 길."""
        goal = self.latest["id"]
        best = None
        q = deque([(start_id, [])])
        seen_depth = {start_id: 0}
        while q:
            node, path = q.popleft()
            if best and len(path) > len(best):
                break
            if node == goal and path:
                if best is None or self._cost(path) < self._cost(best):
                    best = path
                continue
            for p in self.patches:
                if p["from"] == node:
                    d = len(path) + 1
                    if p["to"] not in seen_depth or seen_depth[p["to"]] >= d:
                        seen_depth[p["to"]] = d
                        q.append((p["to"], path + [p]))
        return best

    def _cost(self, path):
        tot = 0
        for p in path:
            try:
                tot += os.path.getsize(os.path.join(self.dir, p["file"]))
            except OSError:
                tot += 1 << 40
        return tot

    def patch_file(self, p):
        fp = os.path.join(self.dir, p["file"])
        if not os.path.isfile(fp):
            raise PatchError(f"패치 파일이 없습니다:\n{fp}\n\n패처 exe 옆의 patch 폴더를 확인해 주세요.")
        return fp

    def cue_text(self, im):
        fp = os.path.join(self.dir, im.get("cue", ""))
        if not im.get("cue") or not os.path.isfile(fp):
            raise PatchError(f"{im['label']} 의 cue 정보 파일이 patch 폴더에 없습니다.")
        return read_text(fp)

    def expected_text(self):
        lines = ["지원하는 원본 (Redump 기준, 트랙을 하나로 합친 bin 값):"]
        for o in self.originals():
            lines.append(f"・{o['name']} ({o.get('serial', '')}) — {o['label']}\n"
                         f"  크기 {o['size']:,} 바이트 / CRC32 {o['crc32']}\n  SHA-256 {o['sha256']}")
        kos = self.koreans()
        lines.append("\n업데이트할 수 있는 이전 한글판 (BIN/CUE 또는 CHD):")
        for k in kos:
            lines.append(f"・{k['label']} — 크기 {k['size']:,} 바이트 / CRC32 {k['crc32']}")
        return "\n".join(lines)


# ---------------------------------------------------------------- cue
def read_text(path):
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "cp932", "mbcs", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("latin-1")


def msf_to_frames(s):
    m, sec, f = (int(x) for x in s.split(":"))
    return (m * 60 + sec) * 75 + f


def frames_to_msf(n):
    return "%02d:%02d:%02d" % (n // 4500, (n // 75) % 60, n % 75)


def parse_cue(path):
    """[{'path': 절대경로, 'tracks': [{'num', 'mode', 'index': [(번호, 프레임)]}]}]"""
    base = os.path.dirname(os.path.abspath(path))
    files = []
    for line in read_text(path).splitlines():
        s = line.strip()
        if not s:
            continue
        m = re.match(r'FILE\s+"(.+)"\s+(\S+)$', s, re.I) or re.match(r'FILE\s+(\S+)\s+(\S+)$', s, re.I)
        if m:
            fp = m.group(1)
            if not os.path.isabs(fp):
                fp = os.path.join(base, fp)
            files.append({"path": fp, "type": m.group(2).upper(), "tracks": []})
            continue
        m = re.match(r"TRACK\s+(\d+)\s+(\S+)", s, re.I)
        if m:
            if not files:
                raise PatchError("cue 파일 형식이 올바르지 않습니다 (FILE 줄 없음).")
            files[-1]["tracks"].append({"num": int(m.group(1)), "mode": m.group(2).upper(), "index": []})
            continue
        m = re.match(r"INDEX\s+(\d+)\s+(\d+:\d+:\d+)", s, re.I)
        if m and files and files[-1]["tracks"]:
            files[-1]["tracks"][-1]["index"].append((int(m.group(1)), msf_to_frames(m.group(2))))
    if not files:
        raise PatchError("cue 파일에서 FILE 항목을 찾지 못했습니다.")
    for f in files:
        if f["type"] != "BINARY":
            raise PatchError(f"지원하지 않는 cue 파일 형식입니다 ({f['type']}). BINARY 만 지원합니다.")
        if not os.path.isfile(f["path"]):
            raise PatchError(f"cue 가 가리키는 파일이 없습니다:\n{f['path']}")
    return files


def merged_cue_text(files, bin_name):
    """분할 cue → 하나의 bin 을 가리키는 cue 문자열 (INDEX 시간을 누적 오프셋만큼 이동)."""
    out = [f'FILE "{bin_name}" BINARY']
    off = 0
    for f in files:
        size = os.path.getsize(f["path"])
        if size % SECTOR:
            raise PatchError(f"bin 크기가 2352 바이트 섹터의 배수가 아닙니다:\n{f['path']}")
        for t in f["tracks"]:
            out.append("  TRACK %02d %s" % (t["num"], t["mode"]))
            for i, fr in t["index"]:
                out.append("    INDEX %02d %s" % (i, frames_to_msf(fr + off)))
        off += size // SECTOR
    return "\n".join(out) + "\n"


def find_cue_for_bin(bin_path):
    """bin 을 직접 골랐을 때, 같은 폴더에서 그 bin 을 가리키는 cue 를 찾는다."""
    d = os.path.dirname(os.path.abspath(bin_path))
    target = os.path.normcase(os.path.abspath(bin_path))
    for n in os.listdir(d):
        if n.lower().endswith(".cue"):
            try:
                files = parse_cue(os.path.join(d, n))
            except Exception:
                continue
            if any(os.path.normcase(os.path.abspath(f["path"])) == target for f in files):
                return os.path.join(d, n)
    return None


# ---------------------------------------------------------------- 외부 도구
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def run_tool(args, cancel=None, on_output=None, poll=None):
    """외부 도구 실행. 출력(\\r / \\n 단위)을 on_output 으로 넘김. 취소 시 종료."""
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
    except OSError as e:
        n = os.path.basename(args[0])
        raise PatchError(f"{n} 을(를) 실행하지 못했습니다: {e.strerror or e}\n\n"
                         f"백신 프로그램이 막았을 수 있습니다. 패처 옆 tools 폴더의 {n} 을(를)\n"
                         "백신의 예외(허용) 목록에 넣은 뒤 다시 시도해 주세요.")
    lines = []

    def reader():
        cur = bytearray()
        while True:
            c = proc.stdout.read(1)
            if not c:
                break
            if c in (b"\r", b"\n"):
                if cur:
                    s = cur.decode("utf-8", "replace")
                    lines.append(s)
                    if on_output:
                        on_output(s)
                    cur = bytearray()
            else:
                cur += c
        if cur:
            s = cur.decode("utf-8", "replace")
            lines.append(s)
            if on_output:
                on_output(s)

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    while True:
        try:
            rc = proc.wait(timeout=0.3)
            break
        except subprocess.TimeoutExpired:
            if cancel and cancel():
                proc.kill()
                proc.wait()
                th.join(2)
                raise Cancelled()
            if poll:
                poll()
    th.join(5)
    return rc, lines


# ---------------------------------------------------------------- chdman.dll (RomForge 래퍼)
def long_path(p):
    p = os.path.abspath(p)
    if p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p


class ChdDll:
    r"""RomForge (https://github.com/sinjunyoung/RomForge, sinjunyoung, GPL-2.0) 의 chdman 래퍼 DLL.
    UTF-8 경로를 그대로 받으므로 한글 경로를 다룰 수 있다. 내보내는 함수 (cdecl):
      int  chdman_create_cd(const char *input, const char *output, const char *compression, progress, log)
      int  chdman_extract_cd(const char *input, const char *output, progress, log)
      int  chdman_get_info(const char *input, log)
      void chdman_cancel(void)
    결과: 0 성공, -1 취소, 1 오류.  progress(int 퍼센트), log(const char *).
    RomForge 의 ChdmanService 처럼 작업 폴더를 입력 파일 폴더로 바꾸고, \\?\ 를 붙인 전체 경로를 넘긴다.
    tools\chdman.dll 이 없거나 실패하면 호출하는 쪽에서 chdman.exe 로 대체한다.
    """
    _inst = None
    _tried = False
    _lock = threading.Lock()
    load_error = None

    @classmethod
    def get(cls):
        if not cls._tried:
            cls._tried = True
            d = os.path.join(app_dir(), "tools")
            p = os.path.join(d, "chdman.dll")
            if os.path.isfile(p):
                try:
                    if hasattr(os, "add_dll_directory"):
                        os.add_dll_directory(d)
                    cls._inst = cls(ctypes.CDLL(p))
                except (OSError, AttributeError) as e:
                    cls.load_error = f"불러오기 실패: {e}"
            else:
                cls.load_error = "tools\\chdman.dll 없음"
        return cls._inst

    def __init__(self, lib):
        self.lib = lib
        self.PROG = ctypes.CFUNCTYPE(None, ctypes.c_int)
        self.LOG = ctypes.CFUNCTYPE(None, ctypes.c_char_p)
        c = ctypes.c_char_p
        lib.chdman_create_cd.argtypes = [c, c, c, self.PROG, self.LOG]
        lib.chdman_create_cd.restype = ctypes.c_int
        lib.chdman_extract_cd.argtypes = [c, c, self.PROG, self.LOG]
        lib.chdman_extract_cd.restype = ctypes.c_int
        lib.chdman_get_info.argtypes = [c, self.LOG]
        lib.chdman_get_info.restype = ctypes.c_int
        lib.chdman_cancel.argtypes = []
        lib.chdman_cancel.restype = None

    def _call(self, invoke, workdir, progress=None, cancel=None):
        lines = []

        def on_log(m):
            if m:
                lines.append(m.decode("utf-8", "replace").rstrip())

        def on_prog(v):
            if progress:
                progress(max(0, min(100, v)) / 100)
        logcb, progcb = self.LOG(on_log), self.PROG(on_prog)
        res = {}

        def body():
            with ChdDll._lock:
                old = os.getcwd()
                try:
                    os.chdir(workdir)
                    res["rc"] = invoke(progcb, logcb)
                except Exception as e:  # noqa
                    res["rc"] = 1
                    lines.append(f"{type(e).__name__}: {e}")
                finally:
                    try:
                        os.chdir(old)
                    except OSError:
                        pass
        th = threading.Thread(target=body, daemon=True)
        th.start()
        cancelled = False
        while th.is_alive():
            th.join(0.3)
            if cancel and not cancelled and cancel():
                cancelled = True
                self.lib.chdman_cancel()
        th.join()
        if cancelled or res.get("rc") == -1:
            raise Cancelled()
        return res.get("rc", 1), lines

    def create_cd(self, cue, out_chd, progress=None, cancel=None):
        i, o = long_path(cue).encode("utf-8"), long_path(out_chd).encode("utf-8")
        return self._call(lambda pc, lc: self.lib.chdman_create_cd(i, o, None, pc, lc),
                          os.path.dirname(os.path.abspath(cue)), progress, cancel)

    def extract_cd(self, chd, out_cue, progress=None, cancel=None):
        i, o = long_path(chd).encode("utf-8"), long_path(out_cue).encode("utf-8")
        return self._call(lambda pc, lc: self.lib.chdman_extract_cd(i, o, pc, lc),
                          os.path.dirname(os.path.abspath(out_cue)), progress, cancel)

    def info(self, chd):
        i = long_path(chd).encode("utf-8")
        return self._call(lambda pc, lc: self.lib.chdman_get_info(i, lc), os.path.dirname(os.path.abspath(chd)))


def parse_info_text(t):
    m1 = re.search(r"^SHA1:\s*([0-9a-fA-F]{40})", t, re.M)
    m2 = re.search(r"^Data SHA1:\s*([0-9a-fA-F]{40})", t, re.M)
    return {"sha1": m1.group(1).lower() if m1 else None,
            "data_sha1": m2.group(1).lower() if m2 else None,
            "cd": "CHT2" in t or "CHTR" in t}


def chd_info(path):
    """CHD 머리글만 읽는다 (빠름). {'sha1', 'data_sha1', 'cd'} 또는 None.
    chdman.dll 이 있으면 그것으로(한글 경로 그대로), 안 되면 chdman.exe 로."""
    dll = ChdDll.get()
    if dll:
        try:
            rc, lines = dll.info(path)
            if rc == 0:
                r = parse_info_text("\n".join(lines))
                if r["sha1"] or r["data_sha1"]:
                    return r
        except Cancelled:
            pass
    return chd_info_exe(path)


def chd_info_exe(path):
    """chdman info 로 CHD 머리글만 읽는다 (빠름). {'sha1', 'data_sha1', 'cd': bool} 또는 None."""
    link = None
    if not is_ascii(os.path.abspath(path)):
        # chdman 0.215 는 영문이 아닌 경로를 못 읽으므로 영문 경로의 하드 링크로 읽는다 (같은 드라이브만)
        drv = os.path.splitdrive(os.path.abspath(path))[0]
        link = (drv + "\\" if drv else "") + "_sgkr_info_%d.chd" % os.getpid()
        try:
            os.link(path, link)
        except OSError:
            return None
    try:
        r = subprocess.run([tool_path("chdman.exe"), "info", "-i", link or path], capture_output=True,
                           stdin=subprocess.DEVNULL, creationflags=NO_WINDOW, timeout=60)
    except Exception:
        return None
    finally:
        if link:
            try:
                os.remove(link)
            except OSError:
                pass
    if r.returncode != 0:
        return None
    return parse_info_text(r.stdout.decode("utf-8", "replace"))


# ---------------------------------------------------------------- 폴더 훑기
def _scan_dirs(folder):
    yield folder
    try:
        for n in sorted(os.listdir(folder)):
            p = os.path.join(folder, n)
            if os.path.isdir(p):
                yield p
    except OSError:
        pass


def cand_label(c, man):
    ims = c["images"]
    if not ims:
        return "판 미확인 CHD (시작하면 풀어서 확인)"
    if len(ims) == 1:
        return ims[0]["label"]
    return " 또는 ".join(i["label"] for i in ims) + " (시작하면 확인)"


def scan_folder(folder, man):
    """
    폴더(하위 폴더 1단계까지)에서 패치할 수 있는 이미지를 찾는다.
    bin/cue 는 크기로만, CHD 는 chdman info 의 SHA1 로 거른다 (해시는 패치할 때 한 번만 계산).
    돌려줌: (후보 목록, 판별 못 한 CHD 목록).
      후보 = {'src', 'images': [manifest 이미지...], 'kind', 'files', 'label', 'latest': bool}
    정렬: 원판·재발매판(manifest 순서) → 한글판(새 버전부터) → 판 미확인 CHD.
    """
    sizes = man.sizes()
    cands, unknown_chds = [], []
    for d in _scan_dirs(folder):
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        referenced = set()
        cues = []
        for n in names:
            p = os.path.join(d, n)
            low = n.lower()
            if not os.path.isfile(p):
                continue
            if low.endswith(".chd"):
                info = chd_info(p)
                if not info:
                    unknown_chds.append(p)
                    continue
                ims = man.by_chd_sha1([info["sha1"], info["data_sha1"]])
                if ims:
                    cands.append({"src": p, "images": ims, "kind": "CHD", "files": 1})
                elif info["cd"]:
                    unknown_chds.append(p)
            elif low.endswith(".cue"):
                try:
                    files = parse_cue(p)
                except Exception:
                    continue
                for f in files:
                    referenced.add(os.path.normcase(os.path.abspath(f["path"])))
                cues.append((p, files))
        for p, files in cues:
            size = sum(os.path.getsize(f["path"]) for f in files)
            if size in sizes:
                kind = (f"트랙별 덤프 (bin {len(files)}개 + cue)" if len(files) > 1
                        else "bin + cue")
                cands.append({"src": p, "images": man.by_size(size), "kind": kind, "files": len(files)})
        for n in names:
            p = os.path.join(d, n)
            if n.lower().endswith(".bin") and os.path.isfile(p) \
                    and os.path.normcase(os.path.abspath(p)) not in referenced:
                size = os.path.getsize(p)
                if size in sizes:
                    cands.append({"src": p, "images": man.by_size(size), "kind": "cue 없는 단일 bin",
                                  "files": 1})
    order = {iid: i for i, iid in enumerate(man.images)}

    def sort_key(c):
        if not c["images"]:
            return (2, 0, 0, c["src"].lower())
        im = c["images"][0]
        if im["kind"] == "original":
            return (0, order[im["id"]], -c["files"], c["src"].lower())
        return (1, tuple(-x for x in vkey(max(vkey(i["version"]) for i in c["images"]))), 0, c["src"].lower())
    cands.sort(key=sort_key)
    for c in cands:
        c["label"] = cand_label(c, man)
        c["latest"] = bool(c["images"]) and all(i["id"] == man.latest["id"] for i in c["images"])
    # 판을 모르는 CHD 도 후보로 (시작하면 풀어서 확인)
    for p in unknown_chds:
        c = {"src": p, "images": [], "kind": "CHD", "files": 1, "latest": False}
        c["label"] = cand_label(c, man)
        cands.append(c)
    return cands, unknown_chds


def not_found_message(folder, man):
    return (f"이 폴더에서 패치할 수 있는 이미지를 찾지 못했습니다.\n{folder}\n"
            "(하위 폴더는 1단계까지 찾습니다)\n\n" + man.expected_text())


# ---------------------------------------------------------------- 해시/병합
class Hasher:
    def __init__(self, md5=False):
        self.crc = 0
        self.sha = hashlib.sha256()
        self.md5 = hashlib.md5() if md5 else None

    def update(self, b):
        self.crc = zlib.crc32(b, self.crc)
        self.sha.update(b)
        if self.md5:
            self.md5.update(b)

    def result(self):
        r = {"crc32": "%08X" % (self.crc & 0xFFFFFFFF), "sha256": self.sha.hexdigest().upper()}
        if self.md5:
            r["md5"] = self.md5.hexdigest().upper()
        return r


def stream_files(paths, out_path, progress=None, cancel=None, md5=False):
    """paths 를 순서대로 읽어(필요하면 out_path 에 이어 쓰며) 해시를 계산. 메모리에 통째로 올리지 않음."""
    total = sum(os.path.getsize(p) for p in paths)
    done = 0
    h = Hasher(md5)
    out = open(out_path, "wb") if out_path else None
    try:
        for p in paths:
            with open(p, "rb") as f:
                while True:
                    if cancel and cancel():
                        raise Cancelled()
                    b = f.read(CHUNK)
                    if not b:
                        break
                    h.update(b)
                    if out:
                        out.write(b)
                    done += len(b)
                    if progress:
                        progress(done, total)
    finally:
        if out:
            out.close()
    r = h.result()
    r["size"] = total
    return r


def hash_file(path, progress=None, cancel=None, md5=False):
    return stream_files([path], None, progress, cancel, md5)


def unknown_image_message(info, man, size_only=False):
    got = f"고른 이미지: 크기 {info['size']:,} 바이트"
    if not size_only:
        got += f", CRC32 {info['crc32']}, SHA-256 {info['sha256']}"
    return ("지원하는 원본이나 한글판이 아닙니다.\n\n" + got + "\n\n" + man.expected_text() +
            "\n\n개조·변환된 이미지, 다른 판본, 잘못 뜬 이미지는 패치할 수 없습니다.")


# ---------------------------------------------------------------- 전체 작업
def free_space(path):
    return shutil.disk_usage(path).free


def safe_name(name):
    name = re.sub(r'[\\/:*?"<>|]', "_", name).strip().rstrip(".")
    return name or DEFAULT_NAME


def matches(info, im):
    if (info["size"], info["crc32"], info["sha256"]) != (im["size"], im["crc32"], im["sha256"]):
        return False
    return not (im.get("md5") and info.get("md5") and info["md5"] != im["md5"])


def is_ascii(p):
    try:
        p.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def make_work_dir(dest_dir, need):
    """
    xdelta·chdman 을 돌릴 임시 폴더. chdman 0.215 는 한글 등 영문이 아닌 글자가 든 경로를
    못 다루므로(경로를 찾지 못함) 항상 영문만 있는 경로를 고른다.
    후보: 저장 폴더(영문 경로일 때) → 저장 드라이브 루트 → 공용 사용자 폴더 → 시스템 임시 폴더.
    """
    tag = "_sgkr_tmp_%d" % os.getpid()
    cands = []
    if is_ascii(dest_dir):
        cands.append(os.path.join(dest_dir, tag))
    drv = os.path.splitdrive(os.path.abspath(dest_dir))[0]
    if drv:
        cands.append(drv + "\\" + tag)
    if os.environ.get("PUBLIC"):
        cands.append(os.path.join(os.environ["PUBLIC"], tag))
    cands.append(os.path.join(tempfile.gettempdir(), tag))
    short = None
    for c in cands:
        if not is_ascii(c):
            continue
        try:
            shutil.rmtree(c, ignore_errors=True)
            os.makedirs(c)
            with open(os.path.join(c, "w.test"), "wb") as f:
                f.write(b"x")
            os.remove(os.path.join(c, "w.test"))
        except OSError:
            continue
        if free_space(c) < need:
            short = c
            shutil.rmtree(c, ignore_errors=True)
            continue
        return c
    if short:
        raise PatchError(f"임시 작업 공간이 부족합니다. 약 {need / 2**30:.1f} GB 의 빈 공간이 필요합니다.")
    raise PatchError("영문 경로로 된 임시 폴더를 만들 수 없습니다.\n"
                     "저장 폴더를 영문 경로로 바꾸거나, 다른 드라이브를 골라 주세요.")


def ascii_alias(path, work, label, log):
    """도구에 넘길 경로가 영문이 아니면 임시 폴더에 하드 링크(안 되면 복사)를 만들어 그 경로를 쓴다."""
    if is_ascii(os.path.abspath(path)):
        return path
    dst = os.path.join(work, label + os.path.splitext(path)[1].lower())
    try:
        os.link(path, dst)
    except OSError:
        log(f"영문이 아닌 경로라 임시 폴더에 복사합니다: {os.path.basename(path)}")
        shutil.copyfile(path, dst)
    return dst


def explain_chdman(lines, rc):
    t = "\n".join(lines).lower()
    if "couldn't find" in t or "file not found" in t or "error opening" in t:
        why = "chdman 이 파일 경로를 열지 못했습니다 (경로의 글자·권한 문제)."
    elif "space" in t or "disk full" in t or "write error" in t:
        why = "디스크 공간이 부족하거나 쓰기에 실패했습니다."
    elif "access" in t or "permission" in t:
        why = "폴더에 쓸 권한이 없습니다."
    else:
        why = f"chdman 이 오류로 끝났습니다 (종료 코드 {rc})."
    detail = [s for s in lines if "% complete" not in s][-4:]
    return why + "\n\nchdman 출력:\n" + "\n".join(detail)


def log_file_path():
    for d in (app_dir(), os.path.join(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()), "SGKR_patcher")):
        try:
            os.makedirs(d, exist_ok=True)
            p = os.path.join(d, "패치기록.txt")
            with open(p, "a", encoding="utf-8"):
                pass
            return p
        except OSError:
            continue
    return None


def check_writable(d):
    p = os.path.join(d, "_sgkr_write_test_%d.tmp" % os.getpid())
    try:
        with open(p, "wb") as f:
            f.write(b"x")
        os.remove(p)
    except OSError as e:
        raise PatchError(f"이 폴더에 파일을 쓸 수 없습니다 (쓰기 권한 없음 또는 읽기 전용):\n{d}\n\n"
                         "다른 저장 폴더를 골라 주세요.")


def run_patch(src, out_dir, name, fmt, log, progress, cancel, man):
    """기록을 화면과 로그 파일(패치기록.txt) 양쪽에 남기며 _run_patch 를 실행."""
    lp = log_file_path()
    lf = open(lp, "w", encoding="utf-8") if lp else None

    def log2(s):
        log(s)
        if lf:
            lf.write(s + "\n")
            lf.flush()
    try:
        log2(time.strftime("[%Y-%m-%d %H:%M:%S] ") + f"패처 {man.latest['label']} / 입력 {src} / 형식 {fmt}")
        return _run_patch(src, out_dir, name, fmt, log2, progress, cancel, man)
    except AlreadyLatest as e:
        log2(str(e))
        raise
    except Cancelled:
        log2("취소됨")
        raise
    except PatchError as e:
        log2("오류: " + str(e))
        if lp:
            e.args = (str(e) + f"\n\n자세한 기록: {lp}",) + tuple(e.args[1:])
        raise
    except Exception as e:
        log2(f"예상하지 못한 오류: {type(e).__name__}: {e}")
        raise
    finally:
        if lf:
            lf.close()


def _run_patch(src, out_dir, name, fmt, log, progress, cancel, man):
    """
    src: .cue / .bin / .chd (원본 또는 이전 한글판)
    fmt: 'bincue' 또는 'chd'
    성공 시 결과 파일 목록. 이미 최신이면 AlreadyLatest, CHD 만 실패하면 ChdFailed(BIN/CUE 는 남김).
    """
    name = safe_name(name)
    if not os.path.isdir(out_dir):
        raise PatchError("저장할 폴더가 없습니다.")
    xdelta = tool_path("xdelta.exe")
    chdman = tool_path("chdman.exe") if (fmt == "chd" or src.lower().endswith(".chd")) else None
    latest = man.latest

    is_chd = src.lower().endswith(".chd")
    if is_chd:
        paths = [src]
        src_size = None
        log(f"입력: CHD {os.path.basename(src)}")
    else:
        if src.lower().endswith(".cue"):
            files = parse_cue(src)
        else:
            cue = find_cue_for_bin(src)
            if cue and len(parse_cue(cue)) > 1:
                log(f"고른 bin 을 가리키는 분할 cue 를 찾아 그것을 씁니다: {os.path.basename(cue)}")
                files = parse_cue(cue)
            else:
                files = [{"path": src, "tracks": []}]
        paths = [f["path"] for f in files]
        src_size = sum(os.path.getsize(p) for p in paths)
        log(f"입력 파일 {len(paths)}개, 합계 {src_size:,} 바이트")
        if src_size not in man.sizes():
            raise PatchError(unknown_image_message({"size": src_size}, man, size_only=True))

    # 예전 한글판일 수 있으면(크기가 한글판과 같거나 CHD) 결과는 입력 자리로 (그 자리 교체)
    src_dir = os.path.dirname(os.path.abspath(src))
    ko_sizes = {k["size"] for k in man.koreans()}
    maybe_ko = is_chd or src_size in ko_sizes
    dest_dir = src_dir if maybe_ko else out_dir
    check_writable(dest_dir)
    need = latest["size"] * 2 + 64 * 1024 * 1024
    if is_chd or len(paths) > 1:
        need += man.max_size()
    if fmt == "chd" or is_chd:
        need += latest["size"]
    tmp = make_work_dir(dest_dir, need)
    log(f"임시 작업 폴더: {tmp} (빈 공간 {free_space(tmp) / 2**30:.2f} GB, 필요 최대 {need / 2**30:.2f} GB)")
    same_drive = os.path.splitdrive(tmp)[0].lower() == os.path.splitdrive(os.path.abspath(dest_dir))[0].lower()
    if not same_drive and free_space(dest_dir) < latest["size"] + (32 << 20):
        raise PatchError(f"저장 폴더의 드라이브에 빈 공간이 부족합니다.\n{dest_dir}")
    parts = []
    try:
        # 1) 입력 → 하나의 bin + 해시
        if is_chd:
            log("CHD 를 임시 폴더에 푸는 중 (chdman)...")
            progress("CHD 풀기", 0)
            merged = os.path.join(tmp, "input.bin")
            ok = False
            dll = ChdDll.get()
            if dll:
                log("  chdman.dll 로 풀기 (한글 경로 그대로)")
                rc, lines = dll.extract_cd(src, os.path.join(tmp, "input.cue"),
                                           lambda f: progress("CHD 풀기", f), cancel)
                for s in lines:
                    log("  chdman.dll: " + s)
                tcue = os.path.join(tmp, "input.cue")
                if rc == 0 and not os.path.isfile(merged) and os.path.isfile(tcue):
                    # 새 chdman 은 cue 로 풀 때 트랙별 bin(--splitbin)으로 내놓으므로 순서대로 하나로 합친다
                    tracks = [f["path"] for f in parse_cue(tcue)]
                    log(f"  트랙 {len(tracks)}개로 풀렸습니다 — 하나로 합칩니다")
                    stream_files(tracks, merged, lambda d, t: progress("CHD 풀기", d / t), cancel)
                    for f in tracks:
                        os.remove(f)
                ok = rc == 0 and os.path.isfile(merged)
                if ok:
                    log("  chdman.dll 로 풀었습니다.")
                else:
                    log(f"  chdman.dll 풀기 실패 (결과 {rc}) — chdman.exe 로 다시 합니다.")
            else:
                log(f"  chdman.dll 을 쓰지 않음 ({ChdDll.load_error}) — chdman.exe 사용")
            if not ok:
                for f in os.listdir(tmp):
                    if f.startswith("input."):
                        os.remove(os.path.join(tmp, f))
                chd_in = ascii_alias(src, tmp, "input_src", log)
                rc, lines = run_tool([chdman, "extractcd", "-f", "-i", chd_in, "-o", os.path.join(tmp, "input.cue"),
                                      "-ob", merged], cancel, on_output=_pct("CHD 풀기", progress))
                for s in lines:
                    if "% complete" not in s:
                        log("  chdman: " + s.strip())
                if rc != 0 or not os.path.isfile(merged):
                    raise PatchError("CHD 를 풀지 못했습니다.\n" + explain_chdman(lines, rc))
                if chd_in != src:
                    os.remove(chd_in)
            if os.path.getsize(merged) not in man.sizes():
                raise PatchError(unknown_image_message({"size": os.path.getsize(merged)}, man, size_only=True))
            log("풀어낸 이미지 확인 중...")
            info = hash_file(merged, lambda d, t: progress("입력 확인", d / t), cancel)
        elif len(paths) > 1:
            merged = os.path.join(tmp, "input_merged.bin")
            log("트랙 파일을 순서대로 하나로 합치는 중...")
            info = stream_files(paths, merged, lambda d, t: progress("병합·확인", d / t), cancel)
        else:
            merged = paths[0]
            log("입력 bin 확인 중...")
            info = hash_file(merged, lambda d, t: progress("입력 확인", d / t), cancel)
        log(f"입력 CRC32 {info['crc32']}")
        log(f"입력 SHA-256 {info['sha256']}")
        cur = man.identify(info)
        if not cur:
            raise PatchError(unknown_image_message(info, man))
        log(f"확인됨: {cur['label']} — {cur['name']}")
        if cur["id"] == latest["id"]:
            raise AlreadyLatest(f"이미 최신 판({latest['label']})입니다.\n업데이트할 것이 없습니다.")

        path = man.find_path(cur["id"])
        if not path:
            raise PatchError(f"{cur['label']} 에서 {latest['label']} 로 가는 패치가 patch 폴더에 없습니다.")
        steps = " → ".join([cur["label"]] + [man.images[p["to"]]["label"] for p in path])
        log(f"적용할 패치 {len(path)}단계: {steps}")
        for p in path:
            man.patch_file(p)

        # 2) 저장할 자리: 예전 한글판이면 그 자리에서 같은 이름·형식으로 교체, 원본이면 새로 만듦
        replace = cur["kind"] == "korean"
        old = []
        if replace:
            if is_chd:
                fmt, finals = "chd", [src]
            elif src.lower().endswith(".cue"):
                base = os.path.splitext(os.path.basename(src))[0]
                fmt, finals = "bincue", [os.path.join(src_dir, base + ".bin"), src]
                old = paths
            else:
                fmt, finals = "bin", [paths[0]]
            log("예전 한글판이므로 그 자리에서 같은 이름·형식으로 최신 판으로 바꿉니다:")
            for f in finals:
                log("  " + f)
        else:
            check_writable(out_dir)
            finals = ([os.path.join(out_dir, name + ".bin"), os.path.join(out_dir, name + ".cue")]
                      if fmt == "bincue" else [os.path.join(out_dir, name + ".chd")])
            for f in finals:
                if os.path.exists(f):
                    raise PatchError(f"같은 이름의 파일이 이미 있습니다:\n{f}\n\n다른 이름을 쓰거나 기존 파일을 옮겨 주세요.")

        # 3) 패치 (여러 단계면 이어서) — 결과는 영문 경로 임시 폴더에
        src_bin = ascii_alias(merged, tmp, "input_src", log)
        for i, p in enumerate(path, 1):
            to = man.images[p["to"]]
            out_bin = os.path.join(tmp, f"step{i}.bin")
            stage = f"패치 {i}/{len(path)}" if len(path) > 1 else "패치 적용"
            log(f"[{i}/{len(path)}] {man.images[p['from']]['label']} → {to['label']} 패치 적용 중 (xdelta)...")
            progress(stage, 0)
            pfile = ascii_alias(man.patch_file(p), tmp, f"patch{i}", log)

            def poll(out_bin=out_bin, to=to, stage=stage):
                try:
                    progress(stage, min(1.0, os.path.getsize(out_bin) / to["size"]))
                except OSError:
                    pass
            rc, lines = run_tool([xdelta, "-d", "-f", "-B", "402653184", "-s", src_bin, pfile, out_bin],
                                 cancel, poll=poll)
            if rc != 0:
                raise PatchError("패치 적용에 실패했습니다 (xdelta 오류).\n" + "\n".join(lines[-5:]))
            for f in (src_bin, pfile):
                if os.path.dirname(os.path.abspath(f)) == os.path.abspath(tmp) and os.path.exists(f):
                    os.remove(f)           # 임시 파일·링크만 지움 (사용자 파일은 그대로)
            log(f"[{i}/{len(path)}] 결과 확인 중...")
            r = hash_file(out_bin, lambda d, t, s=stage: progress(s + " 확인", d / t), cancel, md5=True)
            log(f"결과 CRC32 {r['crc32']} / MD5 {r['md5']}")
            if not matches(r, to):
                raise PatchError(f"패치 결과가 {to['label']} 의 예상 값과 다릅니다. 입력이나 패치 파일이 "
                                 f"손상되었을 수 있습니다.\n결과 CRC32 {r['crc32']} (예상 {to['crc32']})")
            log(f"{to['label']} 와 정확히 같습니다.")
            src_bin = out_bin

        def cue_for(bname):
            return re.sub(r'^FILE\s+.*$', f'FILE "{bname}.bin" BINARY', man.cue_text(latest), count=1, flags=re.M)

        # 4) 결과 파일: .part 로 만든 뒤 모두 준비되면 마지막에 이름 바꿈 (원래 파일은 그때 교체)
        if fmt == "chd":
            tcue = os.path.join(tmp, "sgkr.cue")
            with open(tcue, "w", encoding="utf-8", newline="\r\n") as f:
                f.write(cue_for(os.path.splitext(os.path.basename(src_bin))[0]))
            tchd = os.path.join(tmp, "out.chd")
            part = finals[0] + ".part"
            log("CHD 로 압축하는 중 (chdman, 몇 분 걸릴 수 있습니다)...")
            progress("CHD 압축", 0)
            dll_ok = False
            dll = ChdDll.get()
            if dll:
                # DLL 은 한글 경로에 바로 쓸 수 있으므로 결과(.part)를 저장 자리에 바로 만든다
                log("  chdman.dll 로 압축 (한글 경로 그대로)")
                parts.append(part)
                rc, lines = dll.create_cd(tcue, part, lambda f: progress("CHD 압축", f), cancel)
                for s in lines:
                    log("  chdman.dll: " + s)
                dll_ok = rc == 0 and os.path.isfile(part)
                if dll_ok:
                    inf = chd_info(part)
                    want = set(latest.get("chd_sha1") or [])
                    got = {inf.get("sha1"), inf.get("data_sha1")} if inf else set()
                    if want and not (want & got):
                        log("  chdman.dll 결과의 SHA1 이 예상과 달라 chdman.exe 로 다시 합니다.")
                        dll_ok = False
                    else:
                        log("  chdman.dll 로 압축했습니다." + (f" CHD SHA1 {inf.get('sha1')}" if inf else ""))
                else:
                    log(f"  chdman.dll 압축 실패 (결과 {rc}) — chdman.exe 로 다시 합니다.")
                if not dll_ok and os.path.exists(part):
                    os.remove(part)
            else:
                log(f"  chdman.dll 을 쓰지 않음 ({ChdDll.load_error}) — chdman.exe 사용")
            if dll_ok:
                rc, lines = 0, []
            else:
                progress("CHD 압축", 0)
                rc, lines = run_tool([chdman, "createcd", "-f", "-i", tcue, "-o", tchd], cancel,
                                     on_output=_pct("CHD 압축", progress))
                for s in lines:
                    if "% complete" not in s:
                        log("  chdman: " + s.strip())
            if not dll_ok and (rc != 0 or not os.path.isfile(tchd)):
                why = explain_chdman(lines, rc)
                # BIN/CUE 결과는 (해시 확인을 마쳤으니) 남겨 둔다
                base = os.path.splitext(finals[0])[0]
                if os.path.exists(base + ".bin") or os.path.exists(base + ".cue"):
                    base += " (BIN)"
                bfin, cfin = base + ".bin", base + ".cue"
                shutil.move(src_bin, bfin + ".part")
                os.replace(bfin + ".part", bfin)
                with open(cfin, "w", encoding="utf-8", newline="\r\n") as f:
                    f.write(cue_for(os.path.splitext(os.path.basename(bfin))[0]))
                log("CHD 만들기에 실패해 BIN/CUE 결과를 남겼습니다: " + bfin)
                raise ChdFailed("CHD 만들기에 실패했습니다.\n" + why +
                                f"\n\n대신 확인을 마친 BIN/CUE 결과를 남겨 두었습니다:\n{bfin}\n{cfin}"
                                + ("\n\n원래 CHD 는 그대로 있습니다." if replace else ""), [bfin, cfin])
            if not dll_ok:
                parts.append(part)
                shutil.move(tchd, part)
            moves = [(part, finals[0])]
        else:
            binp = finals[0]
            parts.append(binp + ".part")
            shutil.move(src_bin, binp + ".part")
            moves = [(binp + ".part", binp)]
            if fmt == "bincue":
                bname = os.path.splitext(os.path.basename(binp))[0]
                parts.append(finals[1] + ".part")
                with open(finals[1] + ".part", "w", encoding="utf-8", newline="\r\n") as f:
                    f.write(cue_for(bname))
                moves.append((finals[1] + ".part", finals[1]))
        if cancel and cancel():
            raise Cancelled()
        keep = {os.path.normcase(os.path.abspath(f)) for f in finals}
        for a, b in moves:
            os.replace(a, b)
        for p in old:          # 예전 판 bin 중 새 이름과 다른 것 (예: 트랙별 bin, 이름이 다른 bin)
            if os.path.normcase(os.path.abspath(p)) not in keep and os.path.exists(p):
                os.remove(p)
                log("예전 파일 지움: " + p)
        progress("완료", 1)
        log(f"예전 한글판을 {latest['label']} 로 바꿨습니다." if replace else f"{latest['label']} 를 만들었습니다.")
        return finals
    except BaseException:
        # 실패·취소: 만들던 .part 만 지움 (원래 파일은 그대로)
        for p in parts:
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass
        raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _pct(stage, progress):
    def f(s):
        m = re.search(r"([\d.]+)%\s*complete", s)
        if m:
            progress(stage, float(m.group(1)) / 100)
    return f
