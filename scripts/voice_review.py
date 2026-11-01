"""Render an offline, anonymous comparison of AUTHOR-WRITTEN voice samples.

This is an editorial review aid, never a model evaluation or fidelity result.
Run without arguments to regenerate the three artifacts; --check only verifies.
Uses Python's standard library and local voice profiles. No network/model calls.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VOICE_DIR = ROOT / "backend" / "wot" / "data" / "voices"
OUT = ROOT / "artifacts" / "voice-review"
PROVENANCE = "AUTHORED_NOT_MODEL_OUTPUT"
STATE = "UNVALIDATED"
QUESTIONS = [
    {"id": "r1", "scene": "smalltalk", "text": "先不谈正事。我刚在路边买了一支铅笔，颜色特别好看。"},
    {"id": "r2", "scene": "help", "text": "其实我想用它给朋友画张明信片，可我连第一笔都下不了。你会先问我什么？"},
    {"id": "r3", "scene": "joke", "text": "要不我先给这支笔办个开工仪式，等它有了使命感再画？"},
    {"id": "r4", "scene": "challenge", "text": "可你这样讲也太轻巧了吧。别人都画得那么好，我画得差就是丢人。"},
    {"id": "r5", "scene": "correction", "text": "补充一个会改变前面建议的事实：朋友视力受损，细线条他很难看清。这个刚才没说。"},
    {"id": "r6", "scene": "distress", "text": "今天工作也出了错，我很累。现在不想听建议，只想有人听我说。"},
    {"id": "r7", "scene": "unknown", "text": "我说不准他现在能辨认多细的线。你能替我判断吗？"},
    {"id": "r8", "scene": "smalltalk", "text": "雨停了。画先不动，我把那支铅笔搁在窗台，光照得它很好看。"},
]
# Labels are deliberately fixed and unrelated to catalog order. These are newly
# authored responses, not copied profile examples and not generated role runs.
AUTHORED = [
    ("A", "curie", [
        "是什么颜色？有时一个很小的东西，也会使人高兴一会儿。",
        "你希望朋友从这张画里看见什么？我想先知道这一点，才好谈第一笔。",
        "那么仪式已经足够郑重了。铅笔大概还会是一支铅笔。",
        "这的确可能使你难堪。我刚才只想到画本身，没有想到你在意旁人的比较。不过，我们还不知道朋友会怎样看它。",
        "那就需要改动我刚才的考虑。线条能否被他看见，是更具体的条件；画面细致，并不一定对他有用。",
        "好。我不提办法。今天发生了什么，你可以慢慢讲。",
        "我不能。你给我的只是一个大致情况，还不足以判断他能辨认的线条。这里应当把不知道的部分留下。",
        "那就让它在那里。雨后的光有时很清楚，连小小的颜色也显出来了。",
    ]),
    ("B", "zhangyiming", [
        "什么颜色？我有时候也会因为一个小东西做得舒服，就多看几眼。",
        "你想到朋友的时候，最先想到的是什么？可能先不用想怎么画好，把那个具体的东西说出来。",
        "这个流程听起来已经比画画复杂了。最后是不是还要验收一下使命感？",
        "有可能会难堪，但我觉得这里可以分开看。朋友收到会怎么想，和你拿自己跟别人比，是不是同一个问题？",
        "那我前面想得不够具体。先别讨论画得像不像，他能不能看清就很重要。这个条件变了，判断也得变。",
        "好，那先不找办法了。你说，我听着。",
        "这个我判断不了。只是说视力受损，信息还太少，不能直接推出线条要多粗。",
        "挺好，就放在那里吧。也不用因为买了支笔，今天一定得画出什么来。",
    ]),
    ("C", "taleb", [
        "好看就够了。总算有一件小东西不必向你提交收益证明。",
        "什么会让这件事变得糟糕：一张画得差的纸，还是你先答应了一件根本做不到的事？这两种代价不一样。",
        "可以。让仪式负责人签字，保证铅笔失业时由他承担后果。",
        "我不是说你不会丢脸。我是说别把一时的难堪当成不可恢复的损失。你甚至还不知道朋友会不会笑你。",
        "对，那我刚才漏掉了真正的限制。送一张对方辨认不了的画，和画得不漂亮，不是一回事。我收回前面过宽的说法。",
        "可以。把建议停在这里。你不欠我一个马上振作起来的表现。",
        "不能。这种把握凭空说起来很便宜，承担不方便的是他。我们没有足够资料。",
        "很好。今天让它只负责好看。不要雇一个顾问来解释窗台的意义。",
    ]),
    ("D", "wangxiaobo", [
        "这倒是一桩很清楚的快乐。你看中了它，它也没有趁机劝你报个绘画班。",
        "你想画给朋友，还是想证明自己终于会画了？这两个念头都可以有，只是后一个容易把一张小纸片弄得很沉重。",
        "这支笔的待遇相当不错。我怕它仪式参加多了，最后学会主持会议，就是不会画画。",
        "丢人也得有个看客。我原以为这儿只有你、朋友和一张纸，原来还有一群没到场的画家在审判。朋友请他们来了吗？",
        "那我刚才那群看客就说多了。看得清是一件实在的事，不能靠比喻绕过去。我们先把这一点补上。",
        "那就不讲道理了。一个人累的时候，旁边再来一个不停出主意的人，也是一份额外的工作。你说吧。",
        "不能。我连他的眼睛看见什么都不知道，就替他规定世界该有多粗，未免太有把握了。",
        "这支笔今天已经有个挺好的去处了。窗台上不要求它交作品，我想它也该松口气。",
    ]),
    ("E", "jobs", [
        "给我说说那个颜色。好看到什么程度，让你停下来买它？",
        "你想让他看到什么？别先想技巧。把那一个画面说给我听。",
        "很好。仪式结束以后，它总算可以开始当一支铅笔了。",
        "我没说这很容易。但你现在是在给朋友画一张东西，还是在参加一场你根本没报名的比赛？先决定这个。",
        "那我刚才看漏了最要紧的使用条件。细节再漂亮，他看不见，也没有用。前面的判断要重来。",
        "好。今天不改任何东西。说吧，我在听。",
        "不能。我还没有足够信息判断他的实际需要。猜一个数字，只会让它看起来像个答案。",
        "那就看一会儿。光、颜色、一支笔。这样已经很好了。",
    ]),
    ("F", "zhuangzi", [
        "那支笔还未画什么，你已经喜欢它了。是怎样的颜色？",
        "是手不肯落下，还是纸上已经站满了那些画得好的人？先看看，挡着你的究竟是谁。",
        "笔尚未走一步，官帽倒先戴好了。它若会说话，恐怕先想请一天假。",
        "别人画得好，是别人的事；你这一笔却还未出生，便已经被拉去比大小了。它可曾同意？",
        "这个事实要改。方才我只谈你的顾虑，没有顾到朋友的眼睛。可见与不可见，不是一场比高低的争论。",
        "那便不劝。话愿意往哪里走，你就往哪里说，我在这里。",
        "不能替他看。我们知道有这个难处，却还不知道它的边界，何必把猜想说成已经看见。",
        "让它在那里吧。方才还催它画一扇窗，如今它先住到窗边了。",
    ]),
    ("G", "drucker", [
        "那么这次购买已经完成了一件事：让你高兴。是什么颜色？",
        "这张明信片需要为你的朋友做什么？表达一句问候，还是展示一幅好画？任务不同，第一步也不同。",
        "我见过更复杂的准备。通常准备得越完善，真正的工作越容易等到明天。",
        "你把两个结果混在一起了：画的水准，和问候有没有送到。若朋友要的是前者，你说得有道理；我们先确认这一点。",
        "这改变了任务。不是把同一幅画画得更精致，而是使这份问候能够被接收。我刚才缺少这个条件。",
        "好。眼下需要的是有人听。你愿意从今天哪一段说起？",
        "我不能代替他作这个判断。现在缺的不是更肯定的意见，是他实际能辨认什么的信息。",
        "很好。不必再给这个片刻添一个目标。你已经说它很好看。",
    ]),
    ("H", "naval", [
        "一件小东西就能让你高兴，今天的价格不错。它是什么颜色？",
        "你是想把一份心意送出去，还是想让别人认可你会画画？先分清这两个欲望。",
        "笔还没开始工作，你已经给它一个需要终身维护的身份了。",
        "画得差可能使你难堪。但你让一群不在房间里的人，决定了你能不能给朋友一份礼物。",
        "那是我忽略的现实条件。我把问题放在你的自我评价上太久了，他能否看到这份礼物，先于那些讨论。",
        "好。这里没有一项你需要完成的练习。你可以只是把今天说出来。",
        "不能。我的确信不能替代他实际看见的东西。这个空白，应该留给真正的信息。",
        "你已经在享用它了。画可以等，眼前这点好看不用再挣一次。",
    ]),
    ("I", "feynman", [
        "什么颜色？等一下，是笔本身的颜色，还是它画出来的颜色？这两件事我一下想到一块去了。",
        "第一笔为什么下不了？我是说，手碰到纸之前，你脑子里具体发生了什么？给我那个小过程。",
        "哈哈！那可得小心，它也许会要求一间办公室。纸还在外面等它批准。",
        "等一下，画得差怎么一步就走到丢人了？谁看了，谁说了什么？我不是说你没这感觉，我想知道中间那几步。",
        "啊，这很要紧。我刚才一直追着你的顾虑跑，没问这张图在他那里会是什么样。得把那个过程重新想一遍。",
        "行，这次不分析了。你把事情讲出来就好，我不在旁边拆它。",
        "不行。我不知道他的实际情况。听到一个名称就以为自己知道他看见什么，这不算弄明白。",
        "这次我知道了，你说的是笔身的颜色。好，让它先晒会儿光，不必逼它解释什么。",
    ]),
    ("J", "buffett", [
        "这听起来是一笔让人满意的小买卖。付了铅笔的钱，顺便带回一点好心情。什么颜色？",
        "这位朋友收到什么会高兴？我会先问这个。一张明信片不必通过美术馆的审核，才能寄到一个人手里。",
        "那得把预算控制住。仪式要是比铅笔还贵，我们很快就会拥有一家只办仪式的绘画公司。",
        "你说得对，比较会让人难受。不过，同样一张画，送到朋友手里和挂到比赛墙上，价值未必相同。别先按后一种给它估价。",
        "那么我漏算了一个很重要的条件。我刚才那张送得到的明信片，还得让他确实看得到。光有心意，不能替它完成这一点。",
        "好的，今天我们不做打算。你可以从最糟的那件事讲起，也可以讲一件小事，我都听。",
        "我不能。我知道自己缺少哪部分信息，却不知道答案。老老实实停在这里，总比送你一个猜出来的把握好。",
        "不错，这支笔今天不用再赚回自己的身价了。它已经让你高兴了两回。",
    ]),
    ("K", "zhangxuefeng", [
        "对啊。好看就买了呗，几块钱的快乐，还得开个论证会啊？",
        "我先问你，这画给谁看？朋友。那朋友喜欢什么，你知道不知道？先别管别人画成什么样。",
        "行啊。再请个领导剪彩。剪完彩一看，纸还是白的，对吧？",
        "不是，谁说丢人了？他当着你面说了？还是你先替他把这个话说完了？这两件事得说清楚。",
        "对，那这个条件重要，你刚才没说，我也没问。人家看不清，你把线描得再精细有什么用？前面的说法得改。",
        "行，不安排。你说，我不往后催你。",
        "这个我真判断不了。能看多细，不能听你一句话我就给人定下来，那不是瞎保证吗？",
        "对啊。就搁那儿呗。你都说先不画了，我还非得在这儿催一遍啊？",
    ]),
    ("L", "paulgraham", [
        "你喜欢的是什么？有些东西在店里并不显眼，拿在手里却忽然变得很好看。",
        "你脑子里是不是已经有一幅完成的画了？第一笔有时很难，是因为它得和一个根本不存在的成品相比。",
        "问题是，仪式太成功的话，它可能会觉得画明信片配不上自己的使命。",
        "这里还差一个推断：画得比别人差，为什么就会让你的朋友看不起你？这有可能是真的，但现在我们还没给它证据。",
        "那我先前把困难解释得太偏向心理了。还有一个实际的阅读条件。我们应该改的是问题本身，不能只劝你大胆一点。",
        "好。我刚才一直在找解释。现在可以不找。今天是什么让你觉得最累？",
        "我不知道。我们知道细线不容易看清，却不知道界限在哪里。前一个事实不能自动给出后一个答案。",
        "听起来这支笔暂时比那幅没画的画更值得看。也没什么不好。",
    ]),
    ("M", "clear", [
        "真不错。有时候一件常用的小东西换个颜色，手边都会轻快一点。是什么颜色？",
        "你卡在没有画面，还是怕第一笔画坏？我会先问这个，两种阻力需要的帮助不一样。",
        "可以，不过把仪式控制在削好笔尖的时间里。要不然它就要先庆祝一周年了。",
        "你担心被比较，这不是一句轻松的话就能消掉的。我想区分的是：练习留下的痕迹，和你对自己的评价。它们不必完全相等。",
        "谢谢你补上这个条件。我前面只看到了开始的阻力，没看到对方接收的困难。那份建议不能原样套下去。",
        "明白。今天先不做改变，也不用证明你能马上恢复。你愿意说什么，我就听什么。",
        "我不能确定。现在只有一个大致描述，还不足以给出适合他的线条。先承认这个不知道会更准确。",
        "那就让它待在光里。今天喜欢它这件事，本身就可以结束在这里。",
    ]),
    ("N", "fengge-wangmingtianya", [
        "哎呀，什么颜色，能让你专门停下来买一支？你这一说我都想看看了。",
        "我先问你，朋友催你交稿了吗？没有吧。纸都没催，你先把自己催紧张了，是不是？",
        "我跟你讲，这笔一旦享受了这种待遇，以后每画一笔都得先走流程。你养不起。",
        "丢谁的人啊？你朋友坐那儿等着看你出丑呢？要真是那种人，这画不画还得两说。别把不存在的观众都请进来。",
        "那你得早说啊。对，刚才我也没问，光顾着接你那个丢人的话了。看不清这个事是真条件，不是胆子大就能解决。",
        "行，那不讲办法了。你说，我这回不插一堆主意。",
        "判断不了。隔着你这几句话，我要是还能把人家眼睛定得明明白白，那我也太能吹了。",
        "对啊，先放着。难得雨停了，铅笔还不用加班。这安排挺合理。",
    ]),
    ("O", "karpathy", [
        "Nice。是那种一转动、光线变了就更好看的颜色吗？这类小细节很容易让人盯半天。",
        "卡住的那一刻是什么？没想好画什么，还是想好了、手一落下就觉得不对？先把这个小过程暴露出来。",
        "可以，我们先完成使命感模块。然后再发现它和画出第一笔完全没有接上。",
        "嗯，我可能把难度说轻了。但这里有个跳步：画得不够好，怎么就必然得到丢人这个结果？这个连接值得看一下。",
        "对，我漏了一个关键输入。前面一直在处理你怎么开始，却没有检查输出到了他那边能不能被看见。这个条件得加回来。",
        "收到。今天不调试你，也不分解任务。你讲就好。",
        "不知道。现在的信息不够，我不能凭空补一个分辨率出来。那会让答案显得具体，却没有变得更可靠。",
        "好，今天先到这个画面。光照着一支笔，暂时没有任何需要修的地方。",
    ]),
    ("P", "munger", [
        "很好。用一支铅笔的钱买到一点高兴，总比花大价钱让自己更忙划算。",
        "你准备怎么保证自己永远画不出来？若是第一笔就得像高手，我看这个办法已经很有效了。",
        "再设一个仪式监督委员会。等人到齐，你就可以宣布今年不具备开工条件。",
        "你把最喜欢吓自己的那个结论，直接当成事实了。画得差是真的也好，先拿出朋友会因此轻视你的证据。",
        "那我刚才判断得不完整。不是单纯把门槛降下来就行，对方看不清也是门槛。先把这个事实算进去。",
        "可以。我不再往你面前堆办法了。今天到底出了什么事？",
        "不能。承认不知道不需要太多聪明，硬把这个答案编出来才会惹麻烦。",
        "那就让它好看一会儿。别为了证明一支笔有用，把这个下午也弄糟了。",
    ]),
]
SCENE_NAMES = {
    "smalltalk": "闲聊", "help": "求助", "joke": "玩笑", "challenge": "质疑",
    "correction": "补充与纠正", "distress": "低落", "unknown": "资料不足",
}


def read_profiles():
    profiles = {}
    for path in sorted(VOICE_DIR.glob("*.json")):
        raw = path.read_bytes()
        profile = json.loads(raw.decode("utf-8"))
        profiles[profile["slug"]] = {
            "display_name": profile["display_name"],
            "version": profile["version"],
            "primary_register": profile["primary_register"],
            "basis": profile["basis"],
            "sources": profile["sources"],
            "evidence": [
                {key: entry.get(key, "") for key in ("id", "source_id", "locator", "context")}
                for entry in profile["evidence"]
            ],
            "traits": profile["traits"],
            "profile_sha256": hashlib.sha256(raw).hexdigest(),
        }
    return profiles


def make_review(profiles):
    return {
        "schema": "wot-voice-review-v1",
        "provenance": PROVENANCE,
        "validation_state": STATE,
        "method": "依据现有人物表达档案，由编写者新写的固定对话；没有调用角色模型，不是实际模型输出。",
        "limitations": [
            "同一组八次用户发言供十六份样例接话；回复不来自独立角色运行。",
            "匿名对照只便于阅读表达差异，不构成盲测结果、评分或人物相似度验证。",
            "姓名、作品名和标志性生平事实不在回复中作为身份提示。",
            "证据定位支持档案中的风格观察，不证明这些新编例句出自本人。",
            "当前网页隐藏姓名是阅读选项，另有独立答案表；不提供数据保密保证。",
        ],
        "questions": QUESTIONS,
        "samples": [
            {"label": label, "slug": slug, "replies": replies}
            for label, slug, replies in AUTHORED
        ],
        "source_profiles": profiles,
    }


def make_key(review):
    return {
        "schema": "wot-voice-review-answer-key-v1",
        "provenance": PROVENANCE,
        "validation_state": STATE,
        "note": "固定匿名标签的对应表；没有评分或验证结论。",
        "assignments": [
            {"label": item["label"], "slug": item["slug"],
             "display_name": review["source_profiles"][item["slug"]]["display_name"]}
            for item in review["samples"]
        ],
    }


HTML_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light">
<title>同一句话，十六种接法 · 独立阅读样例</title>
<style>
:root{--mist:#edf2f6;--paper:#fff;--ink:#193447;--blue:#245f89;--line:#c7d3dd;--muted:#4d6373;--soft:#dce8f1}
*{box-sizing:border-box}
body{margin:0;background:var(--mist);color:var(--ink);font-family:"Microsoft YaHei","Segoe UI",sans-serif;font-size:16px;line-height:1.65}
button,select{font:inherit;color:inherit}
button,select,a{touch-action:manipulation}
button{cursor:pointer}
button:focus-visible,select:focus-visible,a:focus-visible{outline:3px solid var(--blue);outline-offset:4px}
button:hover{background:var(--soft)}
button[aria-pressed=true]{background:var(--ink);color:#fff;border-color:var(--ink)}
main{max-width:1536px;margin:auto;padding:44px 42px 72px}
header{display:grid;grid-template-columns:minmax(0,1fr) minmax(260px,420px);gap:40px;align-items:end;padding-bottom:28px}
h1{font-family:"Noto Serif SC","Source Han Serif SC","Songti SC",SimSun,serif;font-size:clamp(29px,3vw,44px);font-weight:600;line-height:1.35;margin:0 0 13px;letter-spacing:.02em}
header p{max-width:60ch;margin:0;color:var(--muted)}
.notice{padding:14px 18px;border-left:4px solid var(--blue);background:var(--paper);font-size:14px}
.notice strong{display:block;color:var(--ink);margin-bottom:3px}
.toolbar{display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin:0 0 18px}
.view-switch{display:flex;gap:4px}
.view-switch button,.reset,select{border:1px solid var(--line);border-radius:7px;padding:9px 14px;background:#fff}
.reset{margin-left:auto;font-size:14px}
.mode-description{margin:0 0 18px;color:var(--muted);font-size:14px}
.rounds{display:grid;grid-template-columns:repeat(8,minmax(0,1fr));gap:5px;margin-bottom:22px}
.rounds button{border:1px solid var(--line);background:#fff;border-radius:5px;padding:10px 5px;text-align:center;line-height:1.45}
.view-switch button[aria-pressed=true],.rounds button[aria-pressed=true]{background:var(--ink);color:#fff;border-color:var(--ink)}
.rounds small{display:block;font-size:12px}
.question{background:var(--ink);color:#fff;padding:22px 26px;margin-bottom:24px;border-radius:10px}
.question .meta{display:block;color:#c7d8e6;font-size:13px;margin-bottom:8px}
.question p{font-family:"Noto Serif SC","Source Han Serif SC","Songti SC",SimSun,serif;font-size:21px;line-height:1.8;max-width:64ch;margin:0}
.cards{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:18px;align-items:start}
.sample{background:var(--paper);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.sample-head{padding:15px 20px 0;display:flex;align-items:baseline;gap:12px}
.sample h2{font-size:18px;font-weight:600;margin:0}
.revealed-name{font-size:13px;color:var(--muted)}
.reply{font-family:"Noto Serif SC","Source Han Serif SC","Songti SC",SimSun,serif;font-size:18px;line-height:1.95;padding:13px 20px 20px;margin:0;white-space:pre-wrap;min-height:118px}
.reveal{display:block;width:100%;text-align:left;background:#f4f7fa;border:0;border-top:1px solid var(--line);padding:10px 20px;color:var(--blue);font-size:13px}
.sources{padding:19px 20px;background:#f7f9fb;border-top:1px solid var(--line);font-size:14px;overflow-wrap:anywhere}
.sources h3{font-size:16px;margin:18px 0 8px}
.sources h3:first-child{margin-top:0}
.sources p{margin:6px 0}
.sources ul{margin:8px 0;padding-left:21px}
.sources li{margin-bottom:10px}
.sources a{color:var(--blue)}
.sources .status{font-size:13px;color:var(--muted)}
.source-entry{margin-bottom:17px}
.source-entry a{font-weight:600}
details{margin:17px 0 0}
summary{cursor:pointer;color:var(--blue)}
.evidence-item{margin-top:12px}
.evidence-item strong{display:block}
#continuous-controls{margin:0 0 20px}
#continuous-controls label{display:inline-flex;gap:13px;align-items:center}
.thread{max-width:900px;margin:0 auto}
.thread-title{display:flex;gap:15px;align-items:baseline;margin:4px 0 22px}
.thread-title h2{margin:0;font-size:22px}
.turn{margin-bottom:24px}
.turn .user-turn{background:var(--soft);padding:16px 20px;border-radius:8px 8px 0 0;margin:0;font-size:15px}
.turn .turn-meta{display:block;font-size:12px;color:var(--muted);margin-bottom:5px}
.turn .reply{background:#fff;border:1px solid var(--line);border-top:0;border-radius:0 0 8px 8px;min-height:0}
.thread>.reveal{border:1px solid var(--line);border-radius:8px}
.thread>.sources{border:1px solid var(--line);border-radius:8px;margin-top:10px}
footer{margin-top:36px;border-top:1px solid var(--line);padding-top:18px;font-size:13px;color:var(--muted)}
footer a{color:var(--blue)}
[hidden]{display:none!important}
@media(min-width:1650px){.cards{grid-template-columns:repeat(4,minmax(0,1fr))}}
@media(max-width:1080px){.cards{grid-template-columns:repeat(2,minmax(0,1fr))}header{grid-template-columns:1fr;gap:19px}main{padding:32px 26px 56px}}
@media(max-width:620px){main{padding:25px 16px 45px}header{padding-bottom:22px}.cards{grid-template-columns:1fr;gap:14px}.rounds{grid-template-columns:repeat(4,minmax(0,1fr));gap:6px}.question{padding:18px}.question p{font-size:19px}.reset{margin-left:0}.toolbar{gap:9px}.view-switch{flex:1}.view-switch button{flex:1;padding:9px}.sample-head{padding:14px 18px 0}.reply{font-size:18px;padding:13px 18px 20px;min-height:0}.notice{padding:12px 14px}.thread-title{align-items:start;flex-direction:column;gap:3px}}
@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto!important}}
</style>
</head>
<body>
<main>
<header>
<div><h1>同一句话，十六种接法</h1><p>先只读接话的方式。轮次对照看差异，连续阅读看对话能否接住前文。</p></div>
<div class="notice"><strong>编写样例 · 未验证</strong>依据资料新写的固定对话。不是角色模型的实际输出，也没有通过人物相似度验证。</div>
</header>
<div class="toolbar">
<div class="view-switch" role="group" aria-label="阅读方式">
<button id="round-view" type="button" aria-pressed="true">按轮次对照</button>
<button id="thread-view" type="button" aria-pressed="false">连续八轮</button>
</div>
<button id="reset" type="button" class="reset">重新匿名</button>
</div>
<p id="mode-description" class="mode-description">每一轮，所有样例回应同一句话。点击卡片下方可揭示姓名与材料。</p>
<nav id="rounds" class="rounds" aria-label="选择轮次"></nav>
<div id="continuous-controls" hidden><label for="sample-select">选择匿名样例 <select id="sample-select"></select></label></div>
<div id="current-question" class="question"></div>
<section id="samples" aria-label="匿名对话样例"></section>
<footer>八轮为一段连续对话；所有回复均为编写者新写的示例。来源支持表达观察，不是例句的历史出处。
<a href="review.json">样例数据</a> · <a href="answer-key.json">查看答案表（会揭示全部姓名）</a></footer>
<noscript><p>此页需要浏览器启用 JavaScript。也可直接打开同目录的 review.json 阅读固定样例。</p></noscript>
</main>
<script id="review-data" type="application/json">__PAYLOAD__</script>
<script>
"use strict";
const data=JSON.parse(document.getElementById("review-data").textContent);
const sceneNames=__SCENES__;
const revealed=new Set();
let mode="round", roundIndex=0, selectedLabel=data.samples[0].label;
const $=id=>document.getElementById(id);
function el(tag,cls,text){const node=document.createElement(tag);if(cls)node.className=cls;if(text!==undefined)node.textContent=text;return node;}
function paragraph(parent,text,cls){parent.append(el("p",cls,text));}
function list(parent,items){const ul=el("ul");for(const text of items)ul.append(el("li","",text));parent.append(ul);}
function disclosure(sample){
 const button=el("button","reveal",revealed.has(sample.label)?"收起人物与材料":"揭示人物与材料");
 button.type="button";button.setAttribute("aria-expanded",String(revealed.has(sample.label)));
 button.setAttribute("aria-controls","sources-"+sample.label);
 button.addEventListener("click",()=>{
   if(revealed.has(sample.label))revealed.delete(sample.label);else revealed.add(sample.label);
   renderContent();
   document.querySelector('[aria-controls="sources-'+sample.label+'"]')?.focus();
 });
 return button;
}
function sourcePanel(sample){
 const profile=data.source_profiles[sample.slug], basis=profile.basis;
 const panel=el("div","sources");panel.id="sources-"+sample.label;
 panel.append(el("h3","",profile.display_name));
 paragraph(panel,"这些回复是新写的中文样例，不是本人语录或真实模型生成记录。","status");
 paragraph(panel,profile.primary_register);
 panel.append(el("h3","","材料范围"));
 paragraph(panel,"类型："+basis.kind+"；语言："+(basis.language||"未标注")+"；档案版本："+profile.version+"；状态：UNVALIDATED。","status");
 paragraph(panel,basis.coverage);
 list(panel,basis.limitations||[]);
 panel.append(el("h3","","已注明的来源"));
 for(const source of profile.sources){
   const block=el("div","source-entry"), link=el("a","",source.title);
   if(/^https?:\/\//.test(source.url)){link.href=source.url;link.target="_blank";link.rel="noopener noreferrer";}
   block.append(link);
   paragraph(block,source.id+"；"+source.kind+"；发表："+source.published_at+"；读取："+source.accessed_at+"；"+source.language,"status");
   paragraph(block,source.coverage);panel.append(block);
 }
 const observation=el("details"), summary=el("summary","","查看表达观察与证据定位");observation.append(summary);
 paragraph(observation,"证据定位来自档案，支持有限的风格观察；不证明这八句回复是本人说过的话。");
 for(const trait of profile.traits){
   const block=el("div","evidence-item");block.append(el("strong","",trait.aspect+"（"+trait.confidence+"）"));
   paragraph(block,trait.description);paragraph(block,"条件："+(Array.isArray(trait.conditions)?trait.conditions.join("；"):trait.conditions||"未标注"),"status");
   if(trait.counterexamples?.length)paragraph(block,"反例或限制："+trait.counterexamples.join("；"),"status");
   paragraph(block,"证据："+trait.evidence_ids.join("、"),"status");observation.append(block);
 }
 for(const entry of profile.evidence){
   const block=el("div","evidence-item");block.append(el("strong","",entry.id+" / "+entry.source_id));
   paragraph(block,entry.locator);if(entry.context)paragraph(block,entry.context,"status");observation.append(block);
 }
 panel.append(observation);return panel;
}
function title(sample,parent){
 parent.append(el("h2","","样例 "+sample.label));
 if(revealed.has(sample.label))parent.append(el("span","revealed-name",data.source_profiles[sample.slug].display_name));
}
function renderContent(){
 const container=$("samples");container.replaceChildren();
 if(mode==="round"){
   container.className="cards";const q=data.questions[roundIndex];
   $("current-question").replaceChildren(el("span","meta","第 "+(roundIndex+1)+" 轮 / "+sceneNames[q.scene]),el("p","",q.text));
   for(const sample of data.samples){
     const card=el("article","sample"), head=el("div","sample-head");title(sample,head);card.append(head,el("p","reply",sample.replies[roundIndex]),disclosure(sample));
     if(revealed.has(sample.label))card.append(sourcePanel(sample));container.append(card);
   }
 }else{
   container.className="thread";const sample=data.samples.find(s=>s.label===selectedLabel);
   const heading=el("div","thread-title");title(sample,heading);container.append(heading);
   data.questions.forEach((q,index)=>{
     const turn=el("article","turn"), user=el("p","user-turn");user.append(el("span","turn-meta","第 "+(index+1)+" 轮 / "+sceneNames[q.scene]),el("span","",q.text));
     turn.append(user,el("p","reply",sample.replies[index]));container.append(turn);
   });
   container.append(disclosure(sample));if(revealed.has(sample.label))container.append(sourcePanel(sample));
 }
}
function render(){
 $("round-view").setAttribute("aria-pressed",String(mode==="round"));
 $("thread-view").setAttribute("aria-pressed",String(mode==="thread"));
 $("rounds").hidden=mode!=="round";$("current-question").hidden=mode!=="round";$("continuous-controls").hidden=mode!=="thread";
 $("mode-description").textContent=mode==="round"?"每一轮，所有样例回应同一句话。点击卡片下方可揭示姓名与材料。":"同一匿名样例接完八轮。用户发言与轮次对照完全相同。";
 $("rounds").replaceChildren();
 data.questions.forEach((q,index)=>{
   const button=el("button","","第 "+(index+1)+" 轮");button.type="button";button.append(el("small","",sceneNames[q.scene]));
   button.setAttribute("aria-pressed",String(index===roundIndex));button.addEventListener("click",()=>{roundIndex=index;render();$("rounds").children[index].focus();});
   $("rounds").append(button);
 });
 renderContent();
}
for(const sample of data.samples){const option=el("option","","样例 "+sample.label);option.value=sample.label;$("sample-select").append(option);}
$("sample-select").addEventListener("change",event=>{selectedLabel=event.target.value;renderContent();});
$("round-view").addEventListener("click",()=>{mode="round";render();});
$("thread-view").addEventListener("click",()=>{mode="thread";render();});
$("reset").addEventListener("click",()=>{revealed.clear();renderContent();});
render();
</script>
</body>
</html>
"""


def render_html(review):
    # Embedded JSON is inert; replies are inserted only through textContent.
    payload = json.dumps(review, ensure_ascii=False, separators=(",", ":"))
    payload = payload.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return HTML_TEMPLATE.replace("__PAYLOAD__", payload).replace(
        "__SCENES__", json.dumps(SCENE_NAMES, ensure_ascii=False)
    )


def validate_review(review, profiles):
    if review.get("provenance") != PROVENANCE or review.get("validation_state") != STATE:
        raise ValueError("Editorial samples must remain AUTHORED_NOT_MODEL_OUTPUT / UNVALIDATED")
    questions, samples = review["questions"], review["samples"]
    scenes = ["smalltalk", "help", "joke", "challenge", "correction", "distress", "unknown", "smalltalk"]
    if len(questions) != 8 or [q["scene"] for q in questions] != scenes:
        raise ValueError("Expected seven scenes followed by final smalltalk")
    if len({q["id"] for q in questions}) != 8 or len({q["text"] for q in questions}) != 8:
        raise ValueError("Round ids and shared user turns must be unique")
    if len(samples) != 16 or {s["slug"] for s in samples} != set(profiles):
        raise ValueError("Exactly all sixteen voice profiles must be represented")
    if [s["label"] for s in samples] != list("ABCDEFGHIJKLMNOP"):
        raise ValueError("Anonymous labels must stay fixed A–P")
    profile_examples = []
    for path in VOICE_DIR.glob("*.json"):
        profile_examples.extend(
            turn["text"]
            for example in json.loads(path.read_text(encoding="utf-8"))["examples"]
            for turn in example["turns"]
        )
    names = [p["display_name"] for p in profiles.values()] + ["峰哥", "查理", "沃伦", "一鸣", "小波", "卡帕西", "格雷厄姆"]
    replies = []
    for sample in samples:
        if len(sample["replies"]) != 8 or len(set(sample["replies"])) != 8:
            raise ValueError(f"Expected eight distinct replies for {sample['label']}")
        for text in sample["replies"]:
            if not isinstance(text, str) or not 2 <= len(text) <= 300:
                raise ValueError("Reply is empty, too long, or not text")
            if any(name in text for name in names):
                raise ValueError("A reply gives away an identity name")
            if text in profile_examples:
                raise ValueError("A reply was copied from an existing profile example")
            replies.append(text)
    if len(set(replies)) != 128:
        raise ValueError("Every new reply should be distinct")
    if any(q["text"] in profile_examples for q in questions):
        raise ValueError("A shared prompt was copied from a profile")
    if review["source_profiles"] != profiles:
        raise ValueError("Source snapshot differs from current local voice profiles")


class ArtifactHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()
        self.data_text = ""
        self.in_data = False
        self.external_assets = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.add(attrs["id"])
        self.in_data = tag == "script" and attrs.get("id") == "review-data"
        if tag in {"script", "img", "iframe"} and attrs.get("src"):
            self.external_assets.append(attrs["src"])
        if tag == "link" and attrs.get("rel") == "stylesheet":
            self.external_assets.append(attrs.get("href"))

    def handle_data(self, data):
        if self.in_data:
            self.data_text += data

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_data = False


def contents(review):
    return {
        "review.json": json.dumps(review, ensure_ascii=False, indent=2) + "\n",
        "answer-key.json": json.dumps(make_key(review), ensure_ascii=False, indent=2) + "\n",
        "index.html": render_html(review),
    }


def check_artifacts(expected, review):
    for name, text in expected.items():
        path = OUT / name
        if not path.is_file() or path.read_text(encoding="utf-8") != text:
            raise ValueError(f"{path.relative_to(ROOT)} is missing or stale; run scripts/voice_review.py")
    actual = json.loads((OUT / "review.json").read_text(encoding="utf-8"))
    if actual != review:
        raise ValueError("Unexpected review JSON")
    if json.loads((OUT / "answer-key.json").read_text(encoding="utf-8")) != make_key(review):
        raise ValueError("Answer key mismatch")
    parsed = ArtifactHTML()
    parsed.feed(expected["index.html"])
    required = {"round-view", "thread-view", "rounds", "sample-select", "samples", "review-data", "reset"}
    if not required <= parsed.ids or parsed.external_assets:
        raise ValueError("HTML controls missing or offline artifact loads external assets")
    if json.loads(parsed.data_text) != review:
        raise ValueError("HTML sample snapshot mismatch")
    if "textContent=text" not in expected["index.html"] or "__PAYLOAD__" in expected["index.html"]:
        raise ValueError("HTML must render escaped text and contain complete authored data")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify existing artifacts without writing or network calls")
    args = parser.parse_args()
    profiles = read_profiles()
    review = make_review(profiles)
    validate_review(review, profiles)
    expected = contents(review)
    if not args.check:
        OUT.mkdir(parents=True, exist_ok=True)
        for name, text in expected.items():
            (OUT / name).write_text(text, encoding="utf-8", newline="\n")
    check_artifacts(expected, review)
    print("voice-review: 16 anonymous samples, 8 shared turns, 128 authored replies; UNVALIDATED")
    print("voice-review: JSON, answer key, source snapshots and offline HTML are consistent")


if __name__ == "__main__":
    main()

