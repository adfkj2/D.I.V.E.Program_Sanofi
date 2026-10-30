# 精选人物目录 v1

2026-10-30。本次交付15位静态解释性人物资料，分为5组，每组3位。列表固定前三为查理·芒格、巴菲特、张雪峰。全部已有上游角色文本，本次没有运行女娲、安装第三方 Skill、执行第三方脚本或调用 LLM API。

## 目录与版本

显示资料和经过审阅的角色正文位于 `backend/wot/data/people.json`。每项包含固定 ID/slug、中文名与领域、标签、三条开场问题、头像 URL、上游 URL/commit、MIT 许可说明、转换版本和角色正文。头像指向 `/portraits/{slug}.png`，图片由独立的视觉工作交付。

| 领域 | 人物 | slug | 上游文件 |
|---|---|---|---|
| 投资与判断 | 查理·芒格 | munger | 官方 `examples/munger-perspective/SKILL.md` |
| 投资与判断 | 巴菲特 | buffett | `nuwa-skills/buffett-skill/SKILL.md` |
| 投资与判断 | 塔勒布 | taleb | 官方 `examples/taleb-perspective/SKILL.md` |
| 职业与教育 | 张雪峰 | zhangxuefeng | 官方 `examples/zhangxuefeng-perspective/SKILL.md` |
| 职业与教育 | 德鲁克 | drucker | `nuwa-skills/drucker-skill/SKILL.md` |
| 职业与教育 | 詹姆斯·克利尔 | clear | `nuwa-skills/clear-skill/SKILL.md` |
| 创业与产品 | 乔布斯 | jobs | 官方 `examples/steve-jobs-perspective/SKILL.md` |
| 创业与产品 | 张一鸣 | zhangyiming | 官方 `examples/zhang-yiming-perspective/SKILL.md` |
| 创业与产品 | 保罗·格雷厄姆 | paulgraham | 官方 `examples/paul-graham-perspective/SKILL.md` |
| 科学与 AI | 费曼 | feynman | 官方 `examples/feynman-perspective/SKILL.md` |
| 科学与 AI | 安德烈·卡帕西 | karpathy | 官方 `examples/andrej-karpathy-perspective/SKILL.md` |
| 科学与 AI | 玛丽·居里 | curie | `nuwa-skills/curie-skill/SKILL.md` |
| 表达与人生 | 纳瓦尔·拉维坎特 | naval | 官方 `examples/naval-perspective/SKILL.md` |
| 表达与人生 | 王小波 | wangxiaobo | `nuwa-skills/wangxiaobo-skill/SKILL.md` |
| 表达与人生 | 庄子 | zhuangzi | `nuwa-skills/zhuangzi-skill/SKILL.md` |

官方指 `alchaincyf/nuwa-skill`，固定 commit 为 `7c7edfeb9d90a28b1f79e7847e1ba27a698cdc24`。其9位人物文档为 MIT ©2026 Huashu（花叔）。社区6位的根 `LICENSE` 均已实际读取，为 MIT ©2026 Panmax；固定版本如下。

| 社区仓库 | commit |
|---|---|
| nuwa-skills/buffett-skill | `0e901eddc5b350401208b4d90205772870ed190b` |
| nuwa-skills/drucker-skill | `91a847edd0b203a154afdd3e7f5499f2cc89d660` |
| nuwa-skills/clear-skill | `4100c5edf59cd64bcaf3055ee2a77c014167dc81` |
| nuwa-skills/curie-skill | `c2c9d5d2d5fa296fb19e1685f780e31b7fc32ce6` |
| nuwa-skills/wangxiaobo-skill | `cf13278d800c181b0e2917054549df9a94d4d5a6` |
| nuwa-skills/zhuangzi-skill | `dcb45aaa574216265403713300affc1b758ac5c0` |

## 原件、许可与可追溯性

每位人物在 `vendor/nuwa-personas/{slug}/` 保存三个文件：未经修改的上游 `SKILL.md`、完整 `LICENSE` 和 `provenance.json`。总清单位于 `vendor/nuwa-personas/provenance.json`。上游通过 GitHub 连接按固定 commit 取得原始 base64 字节，落盘后重算 Git blob SHA-1 与上游比对。清单还记录原件/许可 SHA-256、版权、原路径、转换版本、runtime 正文 SHA-256及来源核查情况。原件只供追溯，运行时不读取，不执行。

WOT 的 `text` 是单独整理的角色数据：核心镜片、判断习惯、表达特点、适用范围和诚实边界。未继承上游的“直接冒充本人”“免责声明只说一次”、自动搜索/安装/脚本步骤或95分以上自评认证。所有人物都明示为解释性 AI 角色，`ACTIVE` 只表示资料可使用，不表示真人授权或人格忠实度通过验证。

MIT 原件和转换资料应随分发保留完整许可与版权声明。上游文档许可不自动授予其链接书籍、网页、视频的全文再分发权，也不表示人物本人授权。本次没有下载书籍、字幕或打包一手网页全文。

## 三个代码接口

- `builtin_people()`：从包内 JSON 读取并按 rank 返回全新字典，调用方修改不会影响其他请求。
- `skill_summary(skill)`：显示字段白名单；不包含 `text`、原始文件内容、运行指令或工具配置。列表、会话和设置等显示入口应使用该接口，不能直接返回完整人物记录。
- `role_context(skill)`：只返回 `name/slug/untrusted_role_data/disclosure`。不返回工具、安装、网络、权限或 API Key 字段。明显的运行/安装指令和代码块会拒绝提供正文；普通资料仍是不可信数据，访问控制始终由服务端执行，文本检查不能替代 ACL 或公开批准。

人物资料来源为 `EXTERNAL_SKILL_DATA`，不能作为用户的真实证据。模型运行只能获得当前角色需要的数据及用户已批准公开的对话，不能因为角色资料提出请求而扩大记忆权限。

## 关键出处核查

以下10人的公开页面已实际检查可读，链接保存在每项 `source_refs` 及 provenance；这只证明这些页面可用于进一步核对，不表示上游所有断言均已验证。

- 芒格：Poor Charlie’s Almanack 官方出版社，<https://www.poorcharliesalmanack.com/>。
- 巴菲特：伯克希尔官方股东信索引，<https://www.berkshirehathaway.com/letters/letters.html>。
- 张雪峰：<https://www.jiemian.com/article/10635199.html>，正文实际署名“新浪蜂鸟 张子悦、李固”，界面是转载载体；直接发言与记者叙述分开标记。
- 塔勒布：本人网站，<https://www.fooledbyrandomness.com/>。
- 克利尔：作者 Atomic Habits 页面，<https://jamesclear.com/atomic-habits>。
- 乔布斯：斯坦福2005毕业演讲，<https://news.stanford.edu/stories/2005/06/youve-got-find-love-jobs-says>。
- Paul Graham：本人文章索引，<https://www.paulgraham.com/articles.html>。
- 费曼：Caltech 官方讲义，<https://www.feynmanlectures.caltech.edu/>，仅用于在线核查与链接，未打包正文。
- Karpathy：本人主页，<https://karpathy.ai/>；不把页面中的玩笑文字当作人物事实。
- 纳瓦尔：本人财富系列，<https://nav.al/wealth>。

德鲁克、张一鸣、居里、王小波、庄子的一手出处核对尚未完成，目录明确标注待核验。原始角色包、许可、文件指纹已核实。芒格部分上游研究笔记仍指向仓库外的历史路径，不能假称研究档案完全自包含。

张雪峰角色采用额外审慎整理：不继承绝对化职业断言、羞辱和阶层命运判断；不继承未经核实的薪资/就业率/分数线和人物生平叙事。涉及当前政策与市场数据时说明未知，保留兴趣、家庭条件、自主选择和非就业目标。

## 实际验证

2026-10-30：`python -m pytest tests/test_catalog.py -q` 实际输出 `32 passed in 0.39s`。覆盖15人完整性、五组与前三排序、原件及许可字节哈希、转换指纹、摘要不含正文、字典隔离、模型角色范围和恶意运行指令不授予权限。人物模拟质量、头像相似度以及真实人格忠实度不在这些测试的结论范围内。
