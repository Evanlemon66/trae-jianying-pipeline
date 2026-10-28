"""VideoCaptioner 桥接层：剪映云 ASR + 字幕翻译。

通过子进程调用 videocaptioner CLI（独立进程，GPL-3.0 无传染问题）：
- ASR:    videocaptioner transcribe <media> --asr jianying -o out.srt   (免费无Key)
- 翻译:   videocaptioner subtitle <srt> --translator llm --target-language en
          --no-split --layout target-only -o out.srt                    (智谱GLM-4-Flash)

翻译降级链: providers(智谱GLM → 讯飞) → google → 本地Ollama → (pipeline 内 MyMemory)
"""

import os
import re
import sys
import json
import time
import shutil
import subprocess
import urllib.request
import urllib.error

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
CONFIG_PATH = os.path.join(_HERE, "presets.json")
_LEGACY_CONFIG_PATH = os.path.join(_HERE, "vc_config.json")  # 旧版翻译配置文件（兼容读取）

sys.stdout.reconfigure(encoding="utf-8")

_SRT_TIME_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
)


# ────────────────────────── 环境探测 ──────────────────────────

def find_python():
    """返回可用的 python 解释器（优先当前进程，其次常见安装路径）。"""
    candidates = [sys.executable]
    local = os.path.join(
        os.environ.get("LOCALAPPDATA", ""), "Programs", "Python", "Python312", "python.exe"
    )
    candidates.append(local)
    for p in candidates:
        if p and os.path.exists(p):
            return p
    return shutil.which("python") or shutil.which("python3")


def _ensure_ffmpeg_path():
    """videocaptioner 内部 pydub 需要 ffmpeg，把 WinGet 安装目录加入子进程 PATH。"""
    env = os.environ.copy()
    if shutil.which("ffmpeg", path=env["PATH"]):
        return env
    import glob as _glob
    pattern = os.path.join(
        os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Packages",
        "*ffmpeg*", "ffmpeg-*", "bin",
    )
    for p in _glob.glob(pattern):
        env["PATH"] = env["PATH"] + os.pathsep + p
        break
    return env


def load_config():
    """读取翻译配置：presets.json 的 translate 段（优先）→ 旧 vc_config.json（兼容）。

    providers 为按顺序尝试的 OpenAI 兼容端点列表；
    兼容旧的单端点字段（api_key/api_base/model），自动包装成一项。
    """
    cfg = {}
    for path, key in ((CONFIG_PATH, "translate"), (_LEGACY_CONFIG_PATH, None)):
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    d = json.load(f)
                cfg = (d.get(key) or {}) if key else d
            except Exception:
                cfg = {}
            if cfg:
                break

    providers = cfg.get("providers")
    if not providers:
        providers = [{
            "name": "zhipu",
            "api_key": cfg.get("api_key") or os.environ.get("ZHIPU_API_KEY", ""),
            "api_base": cfg.get("api_base", "https://open.bigmodel.cn/api/paas/v4"),
            "model": cfg.get("model", "glm-4.7-flash"),
        }]
    else:
        for p in providers:
            env_key = f"{str(p.get('name', '')).upper()}_API_KEY"
            p.setdefault("api_key", os.environ.get(env_key, ""))
            p.setdefault("api_base", "")
            p.setdefault("model", "")

    cfg["providers"] = providers
    cfg.setdefault("reflect", False)
    return cfg


def llm_configured():
    return any(p.get("api_key") for p in load_config()["providers"])


# ────────────────────────── Ollama 可用性探针 ──────────────────────────
# 背景：demucs 等本地任务常抢占显存，Ollama 生成层会产出空/乱码（软失败），
# 白烧一轮 600s 超时。翻译前先用轻量请求确认服务存活、模型已拉取、
# 且能真实生成非空内容，任一不满足则直接跳过该层并记录原因。

_OLLAMA_PROBE = {"ts": 0.0, "ok": False, "why": "未探测"}


def ollama_available(host=None, model=None, ttl=300):
    """轻量探测本地 Ollama 可用性，结果按 ttl 秒缓存（避免每集重复探测）。
    返回 (ok, why)。"""
    cfg = (load_config().get("ollama") or {})
    host = (host or cfg.get("host") or "http://127.0.0.1:11434").rstrip("/")
    model = model or cfg.get("model") or "qwen3:8b"

    now = time.time()
    if now - _OLLAMA_PROBE["ts"] < ttl:
        return _OLLAMA_PROBE["ok"], _OLLAMA_PROBE["why"]
    ok, why = _probe_ollama(host, model)
    _OLLAMA_PROBE.update({"ts": now, "ok": ok, "why": why})
    if not ok:
        print(f"  [ollama] 探针不可用: {why}（{ttl}s 内跳过本层）")
    return ok, why


def _probe_ollama(host, model):
    """两级探测：①服务+模型列表 ②真实生成非空内容。返回 (ok, why)。"""
    try:
        with urllib.request.urlopen(host + "/api/tags", timeout=8) as resp:
            tags = json.loads(resp.read().decode("utf-8"))
        names = {m.get("name") for m in tags.get("models", [])}
        if model not in names and f"{model}:latest" not in names:
            return False, f"模型未拉取: {model}"
    except Exception as e:
        return False, f"服务不可达: {str(e)[:80]}"

    payload = {
        "model": model,
        "prompt": "Reply with the single word: OK",
        "stream": False,
        "think": False,
        "options": {"temperature": 0, "num_predict": 8},
    }
    req = urllib.request.Request(
        host + "/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return False, f"生成探测失败: {str(e)[:80]}"
    if data.get("error"):
        return False, f"生成探测错误: {str(data['error'])[:80]}"
    if not (data.get("response") or "").strip():
        return False, "生成探测返回空（疑显存被抢占）"
    return True, "ok"


# ────────────────────────── SRT 解析/生成 ──────────────────────────

def parse_srt(path):
    """解析 SRT → [{start_us, end_us, text}]。容错：缺序号、时间跨行。"""
    with open(path, "r", encoding="utf-8-sig") as f:
        raw = f.read()

    blocks = re.split(r"\n\s*\n", raw.strip())
    subs = []
    for block in blocks:
        m = _SRT_TIME_RE.search(block)
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        start_us = (g[0] * 3600 + g[1] * 60 + g[2]) * 1_000_000 + g[3] * 1000
        end_us = (g[4] * 3600 + g[5] * 60 + g[6]) * 1_000_000 + g[7] * 1000
        text_part = block[m.end():].strip()
        # 去掉块首可能残留的序号行
        lines = [ln for ln in text_part.splitlines() if ln.strip()]
        if lines and re.fullmatch(r"\d+", lines[0].strip()) and len(lines) > 1:
            lines = lines[1:]
        text = " ".join(ln.strip() for ln in lines).strip()
        if text:
            subs.append({"start_us": start_us, "end_us": end_us, "text": text})
    return subs


def write_srt(subs, path):
    def _fmt(us):
        ms = us // 1000
        h, rem = divmod(ms, 3600_000)
        m, rem = divmod(rem, 60_000)
        s, ms = divmod(rem, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    lines = []
    for i, sub in enumerate(subs, 1):
        lines.append(str(i))
        lines.append(f"{_fmt(sub['start_us'])} --> {_fmt(sub['end_us'])}")
        lines.append(sub["text"])
        lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def texts_to_subs(texts):
    """纯文本列表 → 占位时间轴（每条 3s，用于无时间轴的批量翻译场景）。"""
    return [
        {"start_us": i * 3_000_000, "end_us": (i + 1) * 3_000_000, "text": t}
        for i, t in enumerate(texts)
    ]


# ────────────────────────── 子进程执行 ──────────────────────────

def _run_vc(args, timeout=600):
    py = find_python()
    cmd = [py, "-m", "videocaptioner"] + args
    env = _ensure_ffmpeg_path()
    result = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout, env=env,
    )
    return result


# ────────────────────────── ASR ──────────────────────────

def vc_transcribe(media_path, out_srt, engine="jianying", language=None, timeout=900):
    """剪映云/必剪云 ASR。返回字幕列表；失败返回 None（调用方降级 whisper）。"""
    args = ["transcribe", os.path.abspath(media_path), "--asr", engine,
            "-o", os.path.abspath(out_srt), "-q"]
    if language:
        args += ["--language", language]
    try:
        result = _run_vc(args, timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"  [vc-asr] {engine} 超时")
        return None
    if result.returncode != 0 or not os.path.exists(out_srt):
        tail = (result.stderr or result.stdout or "")[-300:]
        print(f"  [vc-asr] {engine} 失败: {tail}")
        return None
    subs = parse_srt(out_srt)
    print(f"  [vc-asr] {engine} 识别 {len(subs)} 条字幕")
    return subs


def vc_asr_with_fallback(media_path, out_srt, engines=("jianying", "bijian")):
    """ASR 降级链：jianying → bijian。全部失败返回 None。"""
    for engine in engines:
        subs = vc_transcribe(media_path, out_srt, engine=engine)
        if subs:
            return subs
    return None


# ────────────────────────── 翻译 ──────────────────────────

def vc_translate(srt_path, out_srt, translator="llm", target_language="en", timeout=600,
                 provider=None):
    """翻译 SRT（target-only 纯译文输出，时间轴与输入逐条对齐）。
    返回字幕列表；失败返回 None。

    translator=llm 时必须传入 provider（vc_config.json 的 providers 之一）；
    google 免 Key。split 恒关（保持时间轴对齐）。
    """
    args = ["subtitle", os.path.abspath(srt_path),
            "--translator", translator, "--target-language", target_language,
            "--no-split", "--layout", "target-only",
            "-o", os.path.abspath(out_srt), "-q"]

    cfg = load_config()
    if translator == "llm":
        if not provider or not provider.get("api_key"):
            print("  [vc-tr] llm 未配置 api_key (presets.json translate.providers)")
            return None
        args += ["--api-key", provider["api_key"],
                 "--api-base", provider["api_base"], "--model", provider["model"],
                 "--thread-num", str(int(provider.get("thread_num", 2))),
                 "--batch-size", str(int(provider.get("batch_size", 10)))]
        if cfg.get("reflect"):
            args.append("--reflect")
    else:
        # 非免费引擎没有 optimize 步骤；免费引擎必须显式关掉 LLM optimize
        args += ["--no-optimize"]

    try:
        result = _run_vc(args, timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"  [vc-tr] {translator} 超时")
        return None
    if result.returncode != 0 or not os.path.exists(out_srt):
        tail = (result.stderr or result.stdout or "")[-300:]
        print(f"  [vc-tr] {translator} 失败: {tail}")
        return None

    subs = parse_srt(out_srt)
    print(f"  [vc-tr] {translator} 翻译 {len(subs)} 条")
    return subs


CJK_RE = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff01-\uff65]")


def has_cjk(text):
    """译文是否残留中文字符（含全角标点）。"""
    return bool(CJK_RE.search(text or ""))


def _cjk_ratio(text):
    """中文字符占比（用于判断译文是否真的翻译了）。"""
    if not text:
        return 0.0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return cjk / max(1, len(text))


def _parse_numbered(content, n):
    """解析「1. xxx」编号行，条数不符返回空列表。"""
    out = []
    for ln in [x.strip() for x in (content or "").splitlines() if x.strip()]:
        m = re.match(r"^\s*\d+\s*[\.\)、:：]\s*(.+)$", ln)
        if m:
            out.append(m.group(1).strip())
    if len(out) != n:
        lines = [ln.strip() for ln in (content or "").splitlines() if ln.strip()]
        out = lines if len(lines) == n else []
    return out


def _rewrite_prompt(texts, idxs):
    numbered = "\n".join(f"{k + 1}. {texts[i]}" for k, i in enumerate(idxs))
    return (
        "The following English subtitle lines still contain Chinese characters. "
        "Rewrite each line into natural conversational English. Translate every Chinese "
        "word, and render Chinese person/place names in pinyin "
        "(e.g. 建国 → Jianguo). The result must contain no Chinese characters. "
        "Keep the numbering exactly, one line per line, output only numbered lines.\n\n"
        + numbered
    )


def _cjk_to_pinyin(text):
    """把文本中残留的 CJK 连续片段本地转拼音（空格分隔、首字母大写）。
    LLM 回译偶发保留个别汉字（如地名"靠山屯"→"Kaoshan屯"）时的确定性兜底，
    保证返回值零中文。缺 pypinyin 时返回 None（交上层原有兜底）。"""
    try:
        from pypinyin import lazy_pinyin
    except ImportError:
        return None

    def _rep(m):
        return " " + " ".join(w.capitalize() for w in lazy_pinyin(m.group(0))) + " "

    out = re.sub(r"[一-鿿]+", _rep, text)
    out = re.sub(r"\s+", " ", out).strip()
    out = re.sub(r"\s+([.,!?;:])", r"\1", out)  # 收紧标点前的空格（"Tun ." → "Tun."）
    return out if out and not has_cjk(out) else None


def _pinyin_fallback(out):
    """回译结果列表逐行兜底：仍含中文的行本地转拼音，保证零中文。"""
    if not out:
        return out
    fixed = []
    for t in out:
        if t and has_cjk(t):
            py = _cjk_to_pinyin(t)
            if py:
                print(f"  [pinyin兜底] {t[:40]} → {py[:60]}")
                t = py
        fixed.append(t)
    return fixed


def _apply_rewrites(texts, idxs, out):
    """把回译结果并回原列表（仍含中文的保留原文，交给上层兜底）。"""
    if not out:
        return texts
    for i, t in zip(idxs, out):
        if t and not has_cjk(t):
            texts[i] = t
    return texts


def _validate_translation(subs, cn_texts, target_language="en"):
    """校验译文有效性：条数对齐 + 大部分行真的变了 + 中文占比低（软失败检测）。

    subs 既支持 [{"text": ...}] 也支持纯字符串列表。
    """
    if not subs or len(subs) < max(1, int(len(cn_texts) * 0.9)):
        return False
    texts = [s["text"] if isinstance(s, dict) else str(s) for s in subs]
    changed = sum(1 for s, c in zip(texts, cn_texts) if s.strip() != c.strip())
    if changed < len(cn_texts) * 0.5:
        return False
    if target_language.startswith("en"):
        # 译文里中文字符占比过高的行超过 30% 视为软失败
        bad = sum(1 for s in texts if _cjk_ratio(s) > 0.5)
        if bad > len(texts) * 0.3:
            return False
    return True


def _translate_batch_openai(provider, cn_texts, timeout=90):
    """直连 OpenAI 兼容接口，整批一次请求（保留上下文），失败立即返回 None。"""
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(cn_texts))
    prompt = (
        "You are a professional subtitle translator. Translate the following Chinese "
        "subtitles into natural, conversational English suitable for on-screen subtitles. "
        "Translate everything, including names of people and places — render Chinese "
        "names in pinyin (e.g. 建国 → Jianguo). Never leave Chinese characters in the "
        "output. Keep the numbering exactly, one translated line per source line. "
        "Do not merge or split lines. "
        "Output only the numbered translations, no explanations.\n\n" + numbered
    )
    body = {
        "model": provider["model"],
        "messages": [
            {"role": "system", "content": "You translate subtitles faithfully and naturally."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "stream": False,
        # GLM-4-Flash 默认 max_tokens 偏小，长批次（如 36 行对话）会被截断成
        # 半截输出 → 条数校验不过 → 误判软失败。按行数给足输出预算。
        "max_tokens": max(2048, len(cn_texts) * 120),
    }
    req = urllib.request.Request(
        provider["api_base"].rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + provider["api_key"],
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:160]
        print(f"  [llm:{provider['name']}] HTTP {e.code} {detail}")
        return None
    except Exception as e:
        print(f"  [llm:{provider['name']}] 请求失败: {str(e)[:120]}")
        return None

    content = ""
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        print(f"  [llm:{provider['name']}] 响应格式异常: {str(data)[:120]}")
        return None

    lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
    out = []
    for ln in lines:
        m = re.match(r"^\s*\d+\s*[\.\)、]\s*(.+)$", ln)
        if m:
            out.append(m.group(1).strip())
    if len(out) != len(cn_texts):
        out = lines if len(lines) == len(cn_texts) else []
    if not out or _cjk_ratio(" ".join(out)) > 0.2:
        return None
    idxs = [i for i, t in enumerate(out) if has_cjk(t)]
    if idxs:
        print(f"  [llm:{provider['name']}] {len(idxs)} 行残留中文，回译修正")
        out = _apply_rewrites(out, idxs, _rewrite_cjk_openai(provider, out, idxs, timeout))
    return out


def _rewrite_cjk_openai(provider, texts, idxs, timeout=90):
    """对残留中文的译文行做一次定向回译（强制音译人名、禁止中文）。"""
    body = {
        "model": provider["model"],
        "messages": [
            {"role": "system", "content": "You translate subtitles faithfully and naturally."},
            {"role": "user", "content": _rewrite_prompt(texts, idxs)},
        ],
        "temperature": 0.2,
        "stream": False,
    }
    req = urllib.request.Request(
        provider["api_base"].rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + provider["api_key"],
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        content = data["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"  [llm:{provider['name']}] 回译失败: {str(e)[:100]}")
        return []
    return _pinyin_fallback(_parse_numbered(content, len(idxs)))


def _translate_chunked(batch_fn, cn_texts, chunk_size=12):
    """整批翻译失败时按 chunk_size 分批重试。

    长批次（如 36 行对话密集集）模型会截断输出或擅自并行，条数对不上被判软失败；
    小批次指令遵循度高得多。分批间无共享上下文，仅作为整批失败后的兜底。
    """
    out = batch_fn(cn_texts)
    if out:
        return out
    if len(cn_texts) <= chunk_size:
        return None
    print(f"  [chunk] 整批失败，按 {chunk_size} 行分批重试（共 {len(cn_texts)} 行）")
    merged = []
    for off in range(0, len(cn_texts), chunk_size):
        chunk = cn_texts[off:off + chunk_size]
        r = batch_fn(chunk)
        if not r or len(r) != len(chunk):
            return None
        merged += r
    return merged


def translate_texts_via_vc(cn_texts, workdir=None, tag="batch"):
    """中文 → 英文：按 providers 顺序直连 OpenAI 兼容接口（GLM → 讯飞）。

    整批一次请求保留上下文；某项失败（429/超时/软失败）立即换下一项。
    全部失败返回 None，由 pipeline 继续走 google → 本地 Ollama → MyMemory。
    """
    if not cn_texts:
        return None
    for provider in load_config()["providers"]:
        if not provider.get("api_key"):
            continue
        texts = _translate_chunked(lambda t: _translate_batch_openai(provider, t), cn_texts)
        if texts and _validate_translation(texts, cn_texts):
            print(f"  [llm] {provider['name']}({provider['model']}) 翻译 {len(texts)} 条")
            return texts
        elif texts:
            print(f"  [llm] {provider['name']} 软失败(输出无效), 换下一项")
    return None


def _ollama_batch(host, model, cn_texts, timeout=600):
    """Ollama 单批翻译：返回与输入等长的英文列表，失败返回 None。"""
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(cn_texts))
    prompt = (
        "You are a professional subtitle translator. Translate the following Chinese "
        "subtitles into natural, conversational English suitable for on-screen subtitles. "
        "Translate everything, including names of people and places — render Chinese "
        "names in pinyin (e.g. 建国 → Jianguo). Never leave Chinese characters in the "
        "output. Keep the numbering exactly, one translated line per source line. "
        "Do not merge or split lines. "
        "Output only the numbered translations, no explanations.\n\n" + numbered
    )
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        # qwen3 是推理模型：显式关思考链，避免 reasoning 混进译文或拖慢速度
        "think": False,
        "options": {"temperature": 0.2, "num_predict": max(1024, len(cn_texts) * 80)},
    }
    req = urllib.request.Request(
        host + "/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"  [ollama] 请求失败: {str(e)[:120]}")
        return None
    if data.get("error"):
        print(f"  [ollama] {data['error']}")
        return None

    lines = [ln.strip() for ln in (data.get("response") or "").splitlines() if ln.strip()]
    out = []
    for ln in lines:
        m = re.match(r"^\s*\d+\s*[\.\)、]\s*(.+)$", ln)
        if m:
            out.append(m.group(1).strip())
    if len(out) != len(cn_texts):
        out = lines if len(lines) == len(cn_texts) else []
    return out or None


def translate_texts_via_ollama(cn_texts, timeout=600):
    """本地 Ollama 兜底：整批一次请求，保留上下文，返回与输入等长的英文列表。"""
    if not cn_texts:
        return None
    cfg = (load_config().get("ollama") or {})
    if cfg.get("enabled", True) is False:
        return None
    host = (cfg.get("host") or "http://127.0.0.1:11434").rstrip("/")
    model = cfg.get("model") or "qwen3:8b"

    ok, why = ollama_available(host, model)
    if not ok:
        print(f"  [ollama] 探针不可用，跳过本层: {why}")
        return None

    out = _translate_chunked(lambda t: _ollama_batch(host, model, t, timeout), cn_texts)
    if not out or not _validate_translation(out, cn_texts):
        print(f"  [ollama] 输出无效(软失败校验未通过)")
        return None
    idxs = [i for i, t in enumerate(out) if has_cjk(t)]
    if idxs:
        print(f"  [ollama] {len(idxs)} 行残留中文，回译修正")
        out = _apply_rewrites(out, idxs, _rewrite_cjk_ollama(host, model, out, idxs, timeout))
    return out


def _rewrite_cjk_ollama(host, model, texts, idxs, timeout=600):
    """Ollama 定向回译：修掉残留中文（强制音译人名）。"""
    ok, why = ollama_available(host, model)
    if not ok:
        print(f"  [ollama] 探针不可用，跳过回译: {why}")
        return []
    payload = {
        "model": model,
        "prompt": _rewrite_prompt(texts, idxs),
        "stream": False,
        "think": False,
        "options": {"temperature": 0.2, "num_predict": max(256, len(idxs) * 60)},
    }
    req = urllib.request.Request(
        host + "/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"  [ollama] 回译失败: {str(e)[:100]}")
        return []
    if data.get("error"):
        print(f"  [ollama] 回译错误: {data['error']}")
        return []
    return _pinyin_fallback(_parse_numbered(data.get("response") or "", len(idxs)))


# ────────────────────────── CLI 自检 ──────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="VideoCaptioner bridge self-test")
    parser.add_argument("media", help="音视频文件")
    parser.add_argument("--workdir", default=os.path.join(_HERE, "_work"))
    parser.add_argument("--skip-translate", action="store_true")
    args = parser.parse_args()

    os.makedirs(args.workdir, exist_ok=True)
    srt = os.path.join(args.workdir, "bridge_asr.srt")
    subs = vc_asr_with_fallback(args.media, srt)
    if not subs:
        print("ASR 全链失败")
        sys.exit(1)
    for s in subs[:5]:
        print(f"  {s['start_us']/1e6:.2f}-{s['end_us']/1e6:.2f}s  {s['text']}")
    print(f"  ... 共 {len(subs)} 条")

    if not args.skip_translate:
        texts = translate_texts_via_vc([s["text"] for s in subs], args.workdir, tag="bridge")
        if texts:
            print("翻译预览:")
            for cn, en in list(zip([s["text"] for s in subs], texts))[:5]:
                print(f"  {cn}  →  {en}")
        else:
            print("翻译全链失败（llm 未配置 Key 且 bing/google 不可达）")
