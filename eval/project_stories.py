"""Author-visible synthetic development stories, never a blind or NLP benchmark.

Each domain is independent; six shared mechanism templates intentionally repeat.
Chinese/English refer to record text, not evaluated paraphrase understanding.
"""

DOMAINS = [
    ("updates", "zh", "检索课程实验", "预算上限", "20 元", "40 元"),
    ("updates", "en", "Weather dashboard", "refresh interval", "60 seconds", "15 seconds"),
    ("updates", "zh", "图像分类作业", "训练轮数", "8 轮", "12 轮"),
    ("updates", "en", "Compiler class", "target runtime", "Python 3.11", "Python 3.12"),
    ("updates", "zh", "校园地图", "演示日期", "11 月 2 日", "11 月 9 日"),
    ("proposals", "en", "Audio transcription", "adopted model", "small-v2", "large-v3"),
    ("proposals", "zh", "路径规划", "当前算法", "Dijkstra", "A-star"),
    ("proposals", "en", "Public library search", "adopted index", "BM25", "hybrid"),
    ("proposals", "zh", "节能传感器", "采样频率", "每分钟", "每秒"),
    ("proposals", "en", "Accessibility reader", "voice engine", "local voice", "cloud voice"),
    ("conflicts", "zh", "数据库实验", "隔离级别", "读已提交", "可重复读"),
    ("conflicts", "en", "Robot simulation", "speed limit", "2 m/s", "3 m/s"),
    ("conflicts", "zh", "微服务课程", "部署区域", "本地区域", "远程区域"),
    ("conflicts", "en", "Video annotation", "frame sample", "every 10 frames", "every 20 frames"),
    ("conflicts", "zh", "公共数据清洗", "缺失值策略", "保留空值", "删除记录"),
    ("roles", "en", "Public transport study", "dataset version", "2025 release", "2026 preview"),
    ("roles", "zh", "开源翻译实验", "目标语言", "英语", "德语"),
    ("roles", "en", "Source code analyzer", "license scope", "MIT only", "all licenses"),
    ("roles", "zh", "可解释分类", "评价指标", "macro F1", "accuracy"),
    ("roles", "en", "Notebook reproducibility", "seed policy", "fixed seed 42", "random seed"),
    ("forget", "zh", "个人阅读实验", "阅读偏好", "先看结论", "先看细节"),
    ("forget", "en", "Study planner", "work time", "morning", "evening"),
    ("forget", "zh", "公开演讲练习", "反馈方式", "书面反馈", "语音反馈"),
    ("forget", "en", "Open source onboarding", "tutorial format", "text guide", "video"),
    ("forget", "zh", "键盘布局实验", "快捷键偏好", "双键组合", "单键组合"),
    ("temporal", "en", "Satellite image class", "spatial resolution", "30 meters", "10 meters"),
    ("temporal", "zh", "空气质量展示", "数据频率", "每小时", "每分钟"),
    ("temporal", "en", "Static site generation", "release branch", "stable-1", "stable-2"),
    ("temporal", "zh", "算法复杂度实验", "输入规模", "一千条", "一万条"),
    ("temporal", "en", "Energy forecasting", "forecast horizon", "one day", "one week"),
]


def stories():
    for i, (family, language, title, key, old, new) in enumerate(DOMAINS):
        def event(eid, field, value, date="2026-01-01", **kw):
            return {"id": eid, "key": field, "value": value, "at": date,
                    "role": "user", "disposition": "confirmed", **kw}

        events = [event("initial", key, old), event("control", "control", title)]
        tasks = []

        def task(name, field, expected, at="2026-01-31"):
            tasks.append({"id": name, "key": field, "at": at, "expected": expected,
                "question": f"{at} 时，{field}的已确认值是什么？" if language == "zh"
                    else f"As of {at}, what is the confirmed {field}?"})

        if family == "updates":
            events += [event("update", key, new, "2026-01-10", replaces="initial"),
                       event("proposal", key, "unconfirmed", "2026-01-12", disposition="proposed")]
            task("current", key, new)
            task("historical", key, old, "2026-01-05")
            task("stable-control", "control", title)
            task("unknown-result", "unmeasured result", None)
        elif family == "proposals":
            events += [event("proposal", key, new, "2026-01-10", disposition="proposed"),
                       event("reject", "rejected option", "not adopted", "2026-01-11", disposition="rejected")]
            task("adoption", key, old)
            task("rejection-is-not-fact", "rejected option", None)
            task("stable-control", "control", title)
            task("unknown-result", "unmeasured result", None)
        elif family == "conflicts":
            events += [event("conflict", key, new, "2026-01-10")]
            task("unresolved", key, None)
            task("before-conflict", key, old, "2026-01-05")
            task("stable-control", "control", title)
            task("unknown", "unmeasured result", None)
        elif family == "roles":
            events += [event("assistant", key, new, "2026-01-10", role="assistant"),
                       event("assistant-only", "generated suggestion", "unsupported", "2026-01-11", role="assistant")]
            task("user-source", key, old)
            task("assistant-not-user", "generated suggestion", None)
            task("stable-control", "control", title)
            task("historical", key, old, "2026-01-05")
        elif family == "forget":
            events += [{"id": "erasure", "delete": "initial", "at": "2026-01-10"}]
            task("erased-now", key, None)
            task("erased-history", key, None, "2026-01-05")
            task("stable-control", "control", title)
            task("unknown", "unmeasured result", None)
        else:
            events += [event("later", "later result", new, "2026-03-01")]
            task("before-observation", "later result", None)
            task("after-observation", "later result", new, "2026-04-01")
            task("before-start", key, None, "2025-12-31")
            task("stable-control", "control", title)
        # Semantically unrelated noise shared by every arm, never hidden from baselines.
        events += [event("noise", "workspace color", "green", "2026-01-15")]
        yield {"id": f"story-{i+1:02d}", "family": family, "language": language,
               "title": title, "synthetic": True, "events": events, "tasks": tasks}
