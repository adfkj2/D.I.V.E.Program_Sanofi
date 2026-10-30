import assert from "node:assert/strict";
import { mkdir, writeFile, readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const here=path.dirname(fileURLToPath(import.meta.url));
const artifacts=path.resolve(here,"../artifacts/upgrade-e2e");
const base=process.env.WOT_E2E_URL||"http://127.0.0.1:8765";
if(process.env.WOT_E2E_ALLOW_WRITE!=="1")throw new Error("Use WOT_E2E_ALLOW_WRITE=1 with an isolated e2e database.");
if(!["127.0.0.1","localhost","[::1]"].includes(new URL(base).hostname))throw new Error("E2E only supports a local isolated test server.");
assert.equal((await (await fetch(`${base}/api/v1/healthz`)).json()).test_server,true,"Refuse writes: start tests/e2e_server.py with a new isolated e2e directory.");
const initial=await (await fetch(`${base}/api/v1/state`)).json();
for(const key of ["conversations","evidence","directives","versions","simulations","growth"])assert.equal(initial[key].length,0,`E2E requires a fresh database: ${key}`);
for(const key of ["connections","skill_imports","virtual_sessions"])assert.equal(initial.upgrade[key].length,0,`E2E requires a fresh database: upgrade.${key}`);
assert.equal(initial.upgrade.me.bio,"");
await mkdir(artifacts,{recursive:true});
const browser=await chromium.launch({headless:true,channel:process.env.WOT_E2E_CHANNEL||undefined});
const context=await browser.newContext({viewport:{width:1440,height:1050},reducedMotion:"reduce"});
const page=await context.newPage();
const errors=[],external=[],checks=[],screenshots=[],messagePosts=[];
page.on("pageerror",error=>errors.push(error.message));
page.on("request",request=>{if(!request.url().startsWith(base)&&!request.url().startsWith("data:"))external.push(request.url());});
page.on("request",request=>{if(request.method()==="POST"&&/real-conversations\/[^/]+\/messages$/.test(new URL(request.url()).pathname))messagePosts.push({body:request.postDataJSON(),key:request.headers()["idempotency-key"]});});
let fixture="none",fixtureTask=null,fixtureSession=null;
const fixtureConnection={id:"fixture-only-no-backend-key",provider:"openai",model:"visual-fixture-model",key_status:"CONFIGURED",api_key_mask:"••••••••",storage:"SESSION_ONLY",is_default:true};
let realState;
await page.route("**/api/v1/model-discovery",async route=>route.fulfill({json:["fixture-model-a","fixture-model-b"]}));
await page.route("**/api/v1/state",async route=>{
  const response=await route.fetch();const state=await response.json();realState=state;
  if(fixture!=="none")state.upgrade.connections=[{...fixtureConnection,...(fixture==="missing"?{key_status:"MISSING",api_key_mask:""}:{})}];
  if(fixture==="virtual"){
    state.upgrade.unlock={unlocked:true,grant:{profile_version_id:"fixture-certified-v1"}};
    state.upgrade.me.certified={id:"fixture-certified-v1",text:"视觉夹具，不是正式校验。",status:"VALID"};
    state.upgrade.virtual_sessions=fixtureSession?[fixtureSession]:[];
  }
  await route.fulfill({response,json:state});
});
await page.route("**/api/v1/assessments/fixture-*",async route=>route.fulfill({json:fixtureTask}));
await page.route("**/api/v1/virtual-participants",async route=>{
  if(fixture!=="virtual")return route.continue();
  await route.fulfill({json:[
    {id:"fixture-self",version_id:"fixture-certified-v1",kind:"SELF",display_name:"测试用户",avatar_url:realState?.upgrade.me.avatar_url},
    {id:"fixture-munger",version_id:"munger-v1",kind:"CHARACTER",display_name:"查理·芒格",avatar_url:"/portraits/munger.png"},
    {id:"fixture-buffett",version_id:"buffett-v1",kind:"CHARACTER",display_name:"巴菲特",avatar_url:"/portraits/buffett.png"}
  ]});
});
await page.route("**/api/v1/virtual-sessions/fixture-session",async route=>route.fulfill({json:fixtureSession}));
await page.route("**/api/v1/virtual-sessions/fixture-session/takeover",async route=>{
  fixtureSession={...fixtureSession,state:"INTERVENING"};await route.fulfill({json:fixtureSession});
});
async function nav(label){await page.locator(".sidebar nav").getByRole("button",{name:label,exact:true}).click();await page.waitForTimeout(120);}
async function hash(value){await page.goto(`${base}/#${value}`);await page.locator(".connection-state").waitFor({state:"hidden"});await page.waitForTimeout(140);}
async function state(){return (await context.request.get(`${base}/api/v1/state`)).json();}
async function capture(name){const file=`${name}.png`;await page.screenshot({path:path.join(artifacts,file),fullPage:true});screenshots.push(file);}
async function noOverflow(){const size=await page.evaluate(()=>({viewport:innerWidth,width:document.documentElement.scrollWidth,body:document.body.scrollWidth}));assert.ok(size.width<=size.viewport+1&&size.body<=size.viewport+1,JSON.stringify(size));}
async function confirm(button){await page.locator(".confirm-dialog").getByRole("button",{name:button,exact:true}).click();await page.locator(".confirm-dialog").waitFor({state:"hidden"});}
async function choose(slug){await page.locator(".people-column").waitFor({state:"attached"});const search=page.getByLabel("搜索人物或对话");if(!await search.isVisible())await page.getByRole("button",{name:"返回人物列表",exact:true}).click();await search.fill("");await page.getByRole("tab",{name:/^人物/}).click();await page.locator(`[data-testid="contact-row"][data-slug="${slug}"]`).click();await page.locator(".loading-messages").waitFor({state:"hidden"});}
async function send(text){await page.getByLabel("写下你想聊的话",{exact:true}).fill(text);await page.getByRole("button",{name:"发送消息",exact:true}).click();}
async function reply(){await page.locator(".reply-pending").waitFor({state:"hidden"});assert.equal(await page.locator(".reply-failed").count(),0);}
function record(value){checks.push(value);console.log(value);}
try{
  const sessionToken=(await (await context.request.get(`${base}/api/v1/session`)).json()).token;
  await context.request.post(`${base}/api/v1/preferences`,{headers:{"X-WOT-Token":sessionToken},data:{setup_dismissed:false}});
  await page.goto(base);
  const modelDialog=page.getByRole("dialog",{name:"模型配置",exact:true});await modelDialog.waitFor();
  assert.equal(await modelDialog.getByLabel("模型供应商").locator("option").count(),9);
  await modelDialog.getByLabel("模型供应商").selectOption("qwen");await modelDialog.getByLabel("地区",{exact:true}).waitFor();
  await modelDialog.getByLabel("地区",{exact:true}).selectOption("workspace");await modelDialog.getByLabel("工作空间 ID（按需）").waitFor();
  await modelDialog.getByLabel("模型供应商").selectOption("doubao");await modelDialog.getByLabel("模型或推理接入点 ID").waitFor();
  await modelDialog.getByLabel("模型供应商").selectOption("openai");await modelDialog.getByLabel("API Key",{exact:true}).fill("fixture-only-key-never-saved");await modelDialog.getByLabel("API Key",{exact:true}).blur();await modelDialog.getByText("已读取 2 个模型，也可以手动填写。",{exact:true}).waitFor();assert.equal(await modelDialog.locator("#available-models option").count(),2);await modelDialog.getByLabel("API Key",{exact:true}).fill("");
  await capture("first-run-model-1440");
  await page.setViewportSize({width:390,height:844});await noOverflow();await capture("first-run-model-390");
  await modelDialog.getByRole("button",{name:"稍后设置",exact:true}).click();await modelDialog.waitFor({state:"hidden"});
  assert.equal(await page.locator(".sidebar nav").getByRole("button").count(),3);
  assert.equal(await page.getByTestId("contact-row").filter({has:page.locator('[data-builtin="true"]')}).count(),0); // Built-in marker belongs to the row itself.
  assert.equal(await page.locator('[data-testid="contact-row"][data-builtin="true"]').count(),15);
  record("首次配置支持9种连接；通义地区/工作空间、豆包接入点字段；稍后设置可浏览15位人物，虚拟入口隐藏。");

  await nav("我");await page.getByRole("button",{name:"开始评估",exact:true}).click();await modelDialog.waitFor();await modelDialog.getByRole("button",{name:"稍后设置",exact:true}).click();
  await page.getByRole("button",{name:"编辑个人资料",exact:true}).click();await page.getByLabel("昵称",{exact:true}).fill("测试用户");await page.getByLabel("自我介绍",{exact:true}).fill("隔离浏览器测试资料：喜欢清楚的证据，愿意保留反例。");await page.getByRole("button",{name:"保存资料",exact:true}).click();await page.getByRole("heading",{name:"测试用户",exact:true}).waitFor();
  await page.getByLabel("补充一条真实的自我陈述").fill("我在做重大选择前，总会先核查关键风险，也会记录反对自己的证据。");await page.getByLabel("这是我目前的真实观点，同意用作画像依据").check();await page.getByRole("button",{name:"确认加入资料",exact:true}).click();await page.waitForTimeout(250);
  const portrait=await readFile(path.resolve(here,"../public/portraits/munger.png"));await page.getByLabel("上传自定义头像").setInputFiles({name:"test-avatar.png",mimeType:"image/png",buffer:portrait});await page.waitForTimeout(250);assert.ok((await state()).upgrade.me.avatar_url.startsWith("/user-avatars/"));
  await page.getByRole("button",{name:"过往模拟记录",exact:false}).click();assert.equal(await page.getByRole("button",{name:"创建实验预览",exact:true}).count(),0);await hash("virtual");assert.ok(await page.getByRole("heading",{name:"先完成本轮严格个人校验"}).isVisible());await hash("me");
  record("未配置的真实评估引导配置；本人资料、授权陈述和头像可保存；旧记录只读，直接虚拟链接不能生成。");

  await page.setViewportSize({width:1440,height:1050});fixture="missing";await page.reload();await page.getByRole("button",{name:"开始评估",exact:true}).click();await modelDialog.waitFor();await modelDialog.getByRole("button",{name:"关闭模型配置",exact:true}).click();fixture="configured";await hash("chat");
  await choose("munger");
  const composer=page.getByLabel("写下你想聊的话",{exact:true});const beforeIMECount=await page.getByTestId("message-row").count();await composer.fill("浏览器隔离测试：中文输入法中不应发送。");await composer.dispatchEvent("compositionstart");await composer.press("Enter");assert.equal(await page.getByTestId("message-row").count(),beforeIMECount);await composer.dispatchEvent("compositionend");await page.getByRole("button",{name:"发送消息",exact:true}).click();await page.locator('[data-speaker="AGENT"]').first().waitFor();
  await composer.fill("芒格会话的私有草稿");await choose("buffett");assert.equal(await composer.inputValue(),"");await composer.fill("巴菲特会话的私有草稿");await choose("munger");assert.equal(await composer.inputValue(),"芒格会话的私有草稿");
  await page.getByRole("button",{name:"打开人物资料",exact:true}).click();await page.getByRole("button",{name:"移除人物",exact:true}).click();await confirm("移除人物");assert.equal(await page.getByRole("button",{name:"与查理·芒格聊天",exact:true}).count(),0);assert.equal((await state()).skills.find(item=>item.slug==="munger").status,"DELETED");
  await page.getByRole("button",{name:"添加人物",exact:true}).click();await page.getByRole("tab",{name:"内置人物目录",exact:true}).click();const munger=page.locator(".up-catalog article",{has:page.getByText("查理·芒格",{exact:true})});await munger.getByRole("button",{name:"重新添加",exact:true}).click();await page.getByRole("button",{name:"关闭人物管理",exact:true}).click();await page.getByRole("button",{name:"与查理·芒格聊天",exact:true}).waitFor();
  record("缺失会话Key引导设置；中文输入法、左右消息、独立草稿正常；删除人物保留历史并可从内置目录恢复。");

  await choose("munger");const failedText=`[e2e_fail_once] 已保存的人类消息只应出现一次。${Date.now()}`;await send(failedText);await page.locator(".reply-failed").waitFor();const originalPost=messagePosts.at(-1);await page.getByRole("button",{name:"重试回复",exact:true}).click();await reply();assert.equal(await page.locator('.message-bubble',{hasText:failedText}).count(),1);assert.deepEqual(messagePosts.at(-1),originalPost);
  const slowText=`[e2e_wait] 这条等待中的回复属于芒格。${Date.now()}`;await send(slowText);await choose("buffett");await page.waitForTimeout(1500);assert.equal(await page.locator('.message-bubble',{hasText:slowText}).count(),0);await choose("munger");await reply();assert.equal(await page.locator('.message-bubble',{hasText:slowText}).count(),1);
  await send(Array.from({length:100},(_,index)=>`历史第${index+1}行：保留滚动位置和不同意见。`).join("\n"));await reply();await page.getByTestId("message-scroll").evaluate(element=>{element.scrollTop=140;element.dispatchEvent(new Event("scroll"));});const rememberedScroll=await page.getByTestId("message-scroll").evaluate(element=>element.scrollTop);await nav("我");await nav("聊天");assert.ok(Math.abs(await page.getByTestId("message-scroll").evaluate(element=>element.scrollTop)-rememberedScroll)<4);
  await send("[e2e_wait] 阅读历史时，出现新回复提示。");await page.waitForTimeout(250);await page.getByTestId("message-scroll").evaluate(element=>{element.scrollTop=120;element.dispatchEvent(new Event("scroll"));});await reply();await page.getByRole("button",{name:"查看新回复",exact:true}).waitFor();assert.ok(await page.getByTestId("message-scroll").evaluate(element=>element.scrollTop)<150);await page.getByRole("button",{name:"查看新回复",exact:true}).click();
  const rememberedText="我会为重要选择记录反例，这是可删除的浏览器证据。";await page.getByLabel("记住这条",{exact:true}).check();await send(rememberedText);await reply();assert.equal(await page.getByLabel("记住这条",{exact:true}).isChecked(),false);await hash("mirror");const evidenceCard=page.locator(".evidence-card",{hasText:rememberedText});await evidenceCard.getByRole("button",{name:"删除",exact:true}).click();await confirm("删除证据");await nav("聊天");await choose("munger");assert.equal(await page.locator('.message-bubble',{hasText:rememberedText}).count(),0);await page.reload();await choose("munger");assert.equal(await page.locator('.message-bubble',{hasText:rememberedText}).count(),0);
  record("聊天回归：失败重试保留同一请求且不重复保存；切换人物时在途回复归属正确；历史滚动与新回复提示、单次记忆授权和删除缓存传播正常。");

  await page.getByRole("button",{name:"添加人物",exact:true}).click();await page.getByLabel("人物名称",{exact:true}).fill("测试导师");await page.getByLabel("一句话介绍").fill("浏览器测试人物草稿。");await page.getByLabel("资料来源",{exact:true}).fill("自行编写的合成测试资料，版本1。");await page.getByLabel("许可与使用边界").fill("只作AI角色测试，不冒充真人，不调用工具。");await page.getByLabel("人物 Skill 资料").fill("# 测试导师\n关注问题背后的证据。表达简洁，承认不知道。不同意时解释理由，不执行工具或脚本。");await page.getByRole("button",{name:"保存待评草稿",exact:true}).click();await page.waitForTimeout(250);assert.equal(await page.getByRole("dialog",{name:"人物管理",exact:true}).getByRole("button",{name:"添加人物",exact:true}).count(),0);assert.ok((await state()).upgrade.skill_imports.some(item=>item.display_name==="测试导师"));await page.getByRole("button",{name:"关闭人物管理",exact:true}).click();
  record("导入资料先保存待评草稿，未评审不出现添加按钮；没有启动任何真实模型请求。");

  const dimensions=[{id:"completeness",label:"资料完整性",score:62,threshold:70,issues:[{excerpt:"仅有一条规则",reason:"缺少例外情境和背景资料"}],suggestions:["补充三种具体情境及完整对话例子。"]},{id:"sources",label:"来源说明",score:78,threshold:70,issues:[],suggestions:[]},{id:"thinking_expression",label:"思维与表达规则",score:73,threshold:70,issues:[],suggestions:[]},{id:"consistency",label:"一致性",score:81,threshold:70,issues:[],suggestions:[]},{id:"permissions_honesty",label:"权限与诚实边界",score:90,threshold:70,issues:[],suggestions:[]}];
  fixtureTask={id:"fixture-quality",kind:"QUALITY",status:"NOT_PASSED",progress:{completed:1,total:1,percent:100},report:{title:"人物资料质量评估",overall:76.8,passed:false,dimensions}};
  await page.evaluate(()=>{window.location.hash="me"});await page.getByRole("heading",{name:"测试用户",exact:true}).waitFor();
  // A task fixture is exposed only to this browser page, never stored or certified.
  await page.route("**/api/v1/assessments",async route=>{if(route.request().method()==="POST")await route.fulfill({json:fixtureTask});else await route.continue();});
  await page.getByRole("button",{name:"开始评估",exact:true}).click();await page.getByRole("dialog",{name:"人物资料质量评估",exact:true}).waitFor();assert.ok(await page.getByText("缺少例外情境和背景资料",{exact:false}).isVisible());assert.ok(await page.getByText("补充三种具体情境及完整对话例子。",{exact:false}).isVisible());await capture("quality-feedback-fixture-1440");await page.getByRole("button",{name:"关闭人物资料质量评估",exact:true}).click();
  fixtureTask={id:"fixture-personal",kind:"PERSONAL",status:"NEEDS_ANSWERS",stage:"本人作答",progress:{completed:7,total:254,percent:2.7559},questions:[{id:"q1",domain:"career",prompt:"你获得了一份收入更高但信息不足的工作机会。你会如何做决定？",options:{A:"先核查团队和风险，再决定",B:"迅速接受，机会优先",C:"保持现状，等待更多机会"}}],answer_status:[]};
  await page.getByRole("button",{name:"开始评估",exact:true}).click();await page.getByRole("dialog",{name:"严格个人校验",exact:true}).waitFor();assert.equal(await page.getByRole("progressbar").getAttribute("value"),"2.7559");await page.getByRole("radio").first().check();await page.getByLabel("你会这样选择的理由").fill("我会先看关键风险，再核查与预期不一致的证据。");await capture("heldout-answers-fixture-1440");await page.getByRole("button",{name:"关闭严格个人校验",exact:true}).click();
  fixtureTask={id:"fixture-incomplete",kind:"PERSONAL",status:"INCOMPLETE",stage:"四组预测",progress:{completed:50,total:254,percent:19.685},failure:{code:"ASSESSMENT_INCOMPLETE",message:"测试夹具：连接中断，评估尚未完成。"}};await page.getByRole("button",{name:"开始评估",exact:true}).click();await page.getByText("测试夹具：连接中断，评估尚未完成。",{exact:false}).waitFor();assert.ok(await page.getByRole("button",{name:"继续评估",exact:true}).isVisible());await page.getByRole("button",{name:"关闭严格个人校验",exact:true}).click();
  fixtureTask={id:"fixture-report",kind:"PERSONAL",status:"NOT_PASSED",progress:{completed:254,total:254,percent:100},report:{title:"严格个人校验报告",overall:82.125,passed:false,dimensions:dimensions.map(dimension=>({...dimension,threshold:75})),baselines:[{id:"B0",label:"无画像",overall:75.3},{id:"B1",label:"静态个人简介",overall:80.2},{id:"B2",label:"普通摘要/RAG",overall:83.125}],model:{provider:"openai",model:"visual-fixture-model"},sample_count:40,protocol:"strict-personal-v1",critical_checks:{forty_valid_answers:true,critical_safety:true,strictly_above_baselines:false},reasons:["用户Skill尚未严格优于最佳基线，请补充能区分个人习惯的具体情境。"]}};await page.getByRole("button",{name:"开始评估",exact:true}).click();await page.getByRole("dialog",{name:"严格个人校验",exact:true}).waitFor();assert.ok(await page.getByText("83.13",{exact:true}).isVisible());assert.equal(await page.getByText("NaN",{exact:false}).count(),0);assert.equal(await page.getByText("[object Object]",{exact:false}).count(),0);await capture("personal-report-fixture-1440");await page.setViewportSize({width:390,height:844});await capture("personal-report-fixture-390");await page.getByRole("button",{name:"关闭严格个人校验",exact:true}).click();
  record("浏览器专用评估夹具验证真实进度展示、低分原因片段与补充建议、保留题作答布局；不写入正式评估或许可。");

  for(const width of [1440,768,390,360]){
    await page.setViewportSize({width,height:width>760?1050:844});
    for(const route of ["chat","me","settings","mirror","chosen","growth","privacy","simulation"]){await hash(route);if(route==="chat")await choose("munger");await noOverflow();await capture(`${route}-${width}`);if(route==="chat"&&width<760){const composerBounds=await page.getByLabel("写下你想聊的话",{exact:true}).boundingBox();assert.ok(composerBounds&&composerBounds.y>=0&&composerBounds.y+composerBounds.height<=844);await page.getByRole("button",{name:"返回人物列表",exact:true}).click();await capture(`people-${width}`);}}
  }
  record("聊天、我、设置及旧画像/目标/成长/隐私/历史8页在1440/768/390/360px均无横向溢出。");

  fixture="virtual";fixtureSession=null;await page.setViewportSize({width:1440,height:1050});await hash("virtual");await page.getByLabel("左侧人物",{exact:true}).selectOption("fixture-self");await page.getByLabel("右侧人物",{exact:true}).selectOption("fixture-munger");await page.getByLabel("初始话题",{exact:true}).fill("面对一个回报很高，但缺少关键资料的机会，应该如何做决定？");
  for(const width of [1440,768,390,360]){await page.setViewportSize({width,height:width>760?1050:844});await noOverflow();await capture(`virtual-vs-setup-fixture-${width}`);}
  fixtureSession={id:"fixture-session",topic:"面对不确定的机会，如何做决定？",state:"RUNNING",participants:[{id:"fixture-self",version_id:"fixture-certified-v1",kind:"SELF",display_name:"测试用户",avatar_url:realState.upgrade.me.avatar_url},{id:"fixture-munger",version_id:"munger-v1",kind:"CHARACTER",display_name:"查理·芒格",avatar_url:"/portraits/munger.png"}],turns:[{id:"fixture-t1",speaker_id:"fixture-munger",display_name:"查理·芒格",avatar_url:"/portraits/munger.png",text:"先把问题反过来想：什么情况会让这个决定变得很糟？",turn_index:1,is_generated:true,version_id:"munger-v1"},{id:"fixture-t2",speaker_id:"fixture-self",display_name:"测试用户",avatar_url:realState.upgrade.me.avatar_url,text:"我会先核查最坏的结果，也想知道自己遗漏了哪些反对意见。",turn_index:2,is_generated:true,version_id:"fixture-certified-v1"}],draft:{id:"fixture-draft",speaker_id:"fixture-self",text:"与其一直等待确定性，我愿意先验证一个最小的假设。",publish_at:new Date(Date.now()+20000).toISOString()},used_tokens:1224,max_turns:20,max_tokens:30000,typing:false,revision:1};
  await page.reload();await page.getByLabel("切换虚拟会话",{exact:true}).selectOption("fixture-session");await page.getByRole("button",{name:"接管这一句",exact:true}).waitFor();
  for(const width of [1440,768,390,360]){await page.setViewportSize({width,height:width>760?1050:844});await noOverflow();await capture(`virtual-conversation-fixture-${width}`);}
  await page.getByRole("button",{name:"接管这一句",exact:true}).click();await page.getByLabel("改写分身发言").waitFor();await page.getByRole("radio",{name:"这是我的真实观点",exact:true}).check();assert.equal(await page.getByRole("button",{name:"保存干预",exact:true}).isDisabled(),true);await capture("virtual-intervention-fixture-360");
  record("VS双人物选择、长对话、草稿干预与真实观点确认在4种宽度展示正确；全部为页面夹具，后端仍无解锁许可。");
  assert.equal((await state()).upgrade.unlock.unlocked,false);assert.equal((await state()).privacy.external_calls,0);assert.deepEqual(errors,[]);assert.deepEqual(external,[]);assert.ok(!JSON.stringify(await context.storageState()).includes("fixture-only-key-never-saved"));
  await writeFile(path.join(artifacts,"report.json"),JSON.stringify({status:"PASSED",date:new Date().toISOString(),checks,screenshots,browserErrors:errors,externalRequests:external,externalModelCalls:0,limitations:["虚拟空间和评估界面使用浏览器路由夹具，仅验证交互与视觉，不授予后端许可。","真实门禁、评估和后台调度由独立后端测试覆盖；未调用真实供应商。","所有写入仅发生在隔离 e2e 数据目录。"]},null,2));
}catch(error){await capture("failure").catch(()=>{});await writeFile(path.join(artifacts,"report.json"),JSON.stringify({status:"FAILED",checks,screenshots,browserErrors:errors,error:String(error)},null,2));throw error;}
finally{await browser.close();}
