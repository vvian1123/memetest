import os
import json
import random
import asyncio
import time
import re
import aiohttp
import difflib
import zipfile
import io
import datetime
import threading
import smtplib
import sys
import subprocess
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from concurrent.futures import ThreadPoolExecutor
from aiohttp import web
from PIL import Image as PILImage

# --- 自动依赖安装 ---
try:
    import jieba
    import jieba.analyse
    HAS_JIEBA = True
except ImportError:
    print(">>> [Meme] 正在自动安装依赖库 jieba ...", flush=True)
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "jieba"])
        import jieba
        import jieba.analyse
        HAS_JIEBA = True
    except Exception:
        HAS_JIEBA = False

try:
    from lunar_python import Solar
    HAS_LUNAR = True
except ImportError:
    HAS_LUNAR = False

from astrbot.api.star import Context, Star, register
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.core.message.components import Image, Plain

print(">>> [Meme] V3.2 后端核心版 (Logic Fixed) 已加载 <<<", flush=True)

@register("vv_meme_backend", "Vvivloy", "伪向量记忆/拟人分段/邮件备份", "7.2.0")
class MemeMaster(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.base_dir = os.path.abspath(os.path.dirname(__file__))
        self.img_dir = os.path.join(self.base_dir, "images")
        
        # 文件路径
        self.config_file = os.path.join(self.base_dir, "config.json")
        self.meme_index_file = os.path.join(self.base_dir, "memes.json")
        self.sticky_file = os.path.join(self.base_dir, "sticky_notes.json")
        self.fragments_file = os.path.join(self.base_dir, "memory_fragments.json")
        self.old_memory_file = os.path.join(self.base_dir, "memory.txt")
        
        self.executor = ThreadPoolExecutor(max_workers=4)
        self.running = True
        
        if not os.path.exists(self.img_dir): os.makedirs(self.img_dir, exist_ok=True)
        self.local_config = self.load_config()
        if "web_token" not in self.local_config:
            self.local_config["web_token"] = "admin123"
            self.save_config()

        # 加载数据
        self.meme_data = self.load_json(self.meme_index_file)
        self.sticky_notes = self.load_json(self.sticky_file, default_type="list")
        self.fragments = self.load_json(self.fragments_file, default_type="list")
        
        self.chat_buffer = [] 
        self.img_hashes = {} 
        self.sessions = {} 
        self.last_active_time = time.time()
        self.last_uid = None
        self.last_session_id = None
        
        # V2 分段逻辑需要的配置
        self.pair_map = {'“': '”', '《': '》', '（': '）', '(': ')', '"': '"', "'": "'"}
        self.split_chars = "\n。？！?!"

        # 启动任务
        asyncio.create_task(self._system_startup())

    async def _system_startup(self):
        try:
            await self._migrate_legacy_data()
            await self._init_image_hashes()
            await self.start_web_server()
            asyncio.create_task(self._backup_daemon())
            asyncio.create_task(self._lonely_watcher())
            print("✅ [Meme] 后端服务启动成功！等待交互...", flush=True)
        except Exception as e:
            print(f"❌ [Meme] 启动异常: {e}", flush=True)

    def __del__(self):
        self.running = False 

    # ==========================
    # 1. 消息处理主流程
    # ==========================
    @filter.event_message_type(filter.EventMessageType.ALL_MESSAGE, priority=1)
    async def handle_message(self, event: AstrMessageEvent):
        # 自身过滤
        try:
            user_id = str(event.message_obj.sender.user_id)
            if hasattr(self.context, 'get_current_provider_bot'):
                bot = self.context.get_current_provider_bot()
                if bot and user_id == str(bot.self_id): return
        except: pass

        self.check_config_reload()
        msg_str = (event.message_str or "").strip()
        uid = event.unified_msg_origin
        img_urls = self._get_all_img_urls(event)

        if not msg_str and not img_urls: return

        # 状态更新
        self.last_active_time = time.time()
        self.last_uid = uid
        self.last_session_id = event.session_id

        # 自动进货
        if img_urls and not msg_str.startswith("/"):
            self._trigger_auto_save(img_urls, msg_str)

        # 防抖处理
        if await self._handle_debounce(event, uid, msg_str, img_urls):
            return

        # --- RAG 检索开始 ---
        user_keywords = self._extract_keywords(msg_str)
        
        # 1. 小纸条 (Sticky)
        sticky_context = "\n".join([f"- {n}" for n in self.sticky_notes])
        
        # 2. 记忆碎片 (Fragments)
        related_memories = self._search_fragments(user_keywords, top_k=3)
        memory_context = "\n".join([f"- [{m['time']}] {m['content']}" for m in related_memories])
        if related_memories:
            print(f"🧠 [RAG] 命中记忆: {[m['content'][:10] for m in related_memories]}", flush=True)
        
        # 3. 表情包 (Memes)
        candidate_memes = self._search_memes(user_keywords, top_k=5)
        meme_context_list = []
        for fn, info in candidate_memes:
            desc = info.get('tags', '无描述')
            meme_context_list.append(f"ID: <MEME:{fn}> (描述: {desc})")
        if candidate_memes:
            print(f"🧠 [RAG] 命中表情: {len(candidate_memes)} 张", flush=True)
        
        # 4. 构建 Prompt
        time_str = self.get_full_time_str()
        system_prompt = f"""[System Info]
Time: {time_str}
[Rules] (Must Follow)
{sticky_context}
[Related Memories]
{memory_context}
[Available Memes]
{chr(10).join(meme_context_list)}
[Instruction]
Reply naturally. Use <MEME:filename> if appropriate.
To remember a NEW important rule, use <ADD_NOTE>text</ADD_NOTE> at the end."""

        # 注入上下文
        event.message_str = f"{msg_str}\n\n(System: {system_prompt})"
        
        # 记录用户消息到 Buffer
        self.chat_buffer.append(f"User: {msg_str}")
        if len(self.chat_buffer) > 20: 
            asyncio.create_task(self._archive_buffer_to_fragments())

    @filter.on_decorating_result(priority=0)
    async def on_output(self, event: AstrMessageEvent):
        if getattr(event, "__meme_processed", False): return
        result = event.get_result()
        text = self._extract_text_from_result(result)
        if not text: return
        setattr(event, "__meme_processed", True)
        
        # 捕获小纸条 <ADD_NOTE>
        if "<ADD_NOTE>" in text:
            try:
                note = text.split("<ADD_NOTE>")[1].split("</ADD_NOTE>")[0].strip()
                if note and note not in self.sticky_notes:
                    self.sticky_notes.append(note)
                    self.save_json(self.sticky_file, self.sticky_notes)
                    print(f"📌 [Meme] AI 自动记录小纸条: {note}", flush=True)
            except: pass
        
        # 清理 Tag
        text = re.sub(r"<ADD_NOTE>.*?</ADD_NOTE>", "", text)
        clean_log = re.sub(r"<MEME:.*?>", "", text).strip()
        self.chat_buffer.append(f"AI: {clean_log}")
        
        # 解析表情包和文本
        pattern = r"(<MEME:.*?>)"
        parts = re.split(pattern, text)
        chain = []
        
        for part in parts:
            if part.startswith("<MEME:"):
                tag = part[6:-1].strip()
                path = os.path.join(self.img_dir, tag)
                if not os.path.exists(path): path = self.find_best_match(tag)
                if path: 
                    chain.append(Image.fromFileSystem(path))
                    print(f"🎯 [Meme] 发送表情: {tag}", flush=True)
            elif part.strip():
                chain.append(Plain(part))

        if chain:
            event.set_result(None) # 拦截原消息
            
            # --- V2 核心逻辑回归：智能拟人分段 ---
            segments = self.smart_split(chain)
            
            # 读取延迟配置
            delay_base = self.local_config.get("delay_base", 0.5)
            delay_factor = self.local_config.get("delay_factor", 0.1)

            for i, seg in enumerate(segments):
                # 计算延迟
                txt_len = sum(len(c.text) for c in seg if isinstance(c, Plain))
                wait = delay_base + (txt_len * delay_factor)
                
                mc = MessageChain()
                mc.chain = seg
                await self.context.send_message(event.unified_msg_origin, mc)
                
                if i < len(segments) - 1: 
                    await asyncio.sleep(wait)

    # ==========================
    # 2. V2 核心算法：智能分段 (Smart Split) - 完整回归
    # ==========================
    def smart_split(self, chain):
        segs = []; buf = []
        def flush(): 
            if buf: segs.append(buf[:]); buf.clear()
        
        for c in chain:
            if isinstance(c, Image): 
                flush(); segs.append([c]); continue
            
            if isinstance(c, Plain):
                txt = c.text; idx = 0; chunk = ""; stack = []
                while idx < len(txt):
                    char = txt[idx]
                    # 括号配对检测
                    if char in self.pair_map: stack.append(char)
                    elif stack and char == self.pair_map[stack[-1]]: stack.pop()
                    
                    is_split_char = char in self.split_chars
                    force_split = (len(chunk) > 80) # 防止单句过长
                    
                    # 只有在括号外，遇到标点才切分
                    if (not stack and is_split_char) or force_split:
                        chunk += char
                        # 吞掉后面紧跟的标点 (例如 "你好！！")
                        if is_split_char:
                            while idx + 1 < len(txt) and txt[idx+1] in self.split_chars: 
                                idx += 1; chunk += txt[idx]
                        if chunk.strip(): buf.append(Plain(chunk))
                        flush(); chunk = ""
                    else: chunk += char
                    idx += 1
                if chunk: buf.append(Plain(chunk))
        flush(); return segs

    # ==========================
    # 3. 辅助功能 (RAG / Backup / Watcher)
    # ==========================
    async def _lonely_watcher(self):
        """孤独看守者 - 主动聊天"""
        while self.running:
            await asyncio.sleep(60)
            self.check_config_reload()
            
            interval = self.local_config.get("proactive_interval", 0)
            if interval <= 0: continue
            
            # 静默时间判断
            q_start = self.local_config.get("quiet_start", -1)
            q_end = self.local_config.get("quiet_end", -1)
            if q_start != -1 and q_end != -1:
                h = datetime.datetime.now().hour
                is_quiet = False
                if q_start > q_end: 
                    if h >= q_start or h < q_end: is_quiet = True
                else:
                    if q_start <= h < q_end: is_quiet = True
                if is_quiet: continue

            if time.time() - self.last_active_time > (interval * 60):
                self.last_active_time = time.time()
                provider = self.context.get_using_provider()
                if provider and self.last_uid:
                    try:
                        print(f"👋 [Meme] 触发主动聊天...", flush=True)
                        sticky = "\n".join([f"- {n}" for n in self.sticky_notes])
                        prompt = f"""[System]
User silent for {interval} mins. Proactively greet them naturally.
Time: {self.get_full_time_str()}
Context: {sticky}
Keep it short."""
                        resp = await provider.text_chat(prompt, session_id=self.last_session_id)
                        text = (getattr(resp, "completion_text", None) or getattr(resp, "text", "")).strip()
                        if text:
                            # 也要走一遍表情包解析
                            parts = re.split(r"(<MEME:.*?>)", text)
                            chain = []
                            for part in parts:
                                if part.startswith("<MEME:"):
                                    tag = part[6:-1].strip()
                                    path = self.find_best_match(tag)
                                    if path: chain.append(Image.fromFileSystem(path))
                                elif part.strip(): chain.append(Plain(part))
                            
                            if chain:
                                segs = self.smart_split(chain)
                                for s in segs:
                                    mc = MessageChain(); mc.chain = s
                                    await self.context.send_message(self.last_uid, mc)
                                    await asyncio.sleep(1)
                                self.chat_buffer.append(f"AI (Proactive): {text}")
                    except Exception as e:
                        print(f"❌ [Meme] 主动聊天失败: {e}", flush=True)

    # RAG: 关键词提取
    def _extract_keywords(self, text):
        return jieba.analyse.extract_tags(text, topK=10) if HAS_JIEBA and text else []

    # RAG: 搜索碎片
    def _search_fragments(self, kw, top_k=3):
        if not kw: return []
        def jaccard(l1, l2):
            s1, s2 = set(l1), set(l2)
            return len(s1 & s2) / len(s1 | s2) if s1 and s2 else 0.0
        sc = sorted([(jaccard(kw, f.get('keywords',[])), f) for f in self.fragments], key=lambda x:x[0], reverse=True)
        return [i[1] for i in sc[:top_k] if i[0]>0]

    # RAG: 搜索表情
    def _search_memes(self, kw, top_k=5):
        if not kw: return []
        def jaccard(l1, l2):
            s1, s2 = set(l1), set(l2)
            return len(s1 & s2) / len(s1 | s2) if s1 and s2 else 0.0
        sc = sorted([(jaccard(kw, i.get('keywords', [])), f, i) for f,i in self.meme_data.items()], key=lambda x:x[0], reverse=True)
        return [(i[1], i[2]) for i in sc[:top_k] if i[0]>0]

    # RAG: 归档 Buffer
    async def _archive_buffer_to_fragments(self):
        if not self.chat_buffer: return
        p = self.context.get_using_provider()
        if not p: return
        print("🧠 [Meme] 正在归档记忆碎片...", flush=True)
        prompt = f"Extract 1-3 key facts from chat. JSON format: [{{'content':'...','keywords':['...']}}]\nChat:\n" + "\n".join(self.chat_buffer)
        self.chat_buffer = []
        try:
            r = await p.text_chat(prompt, session_id=None)
            t = (getattr(r,"completion_text",None) or getattr(r,"text","")).strip().replace("```json","").replace("```","")
            js = json.loads(t)
            now = self.get_full_time_str()
            for x in js:
                x['time'] = now
                if HAS_JIEBA: x['keywords'] = list(set(x['keywords'] + jieba.analyse.extract_tags(x['content'], topK=3)))
                self.fragments.append(x)
            self.save_json(self.fragments_file, self.fragments)
            print(f"✅ [Meme] 新增 {len(js)} 条记忆", flush=True)
        except: pass

    # 数据迁移
    async def _migrate_legacy_data(self):
        if os.path.exists(self.old_memory_file) and not os.path.exists(self.fragments_file):
            print("📦 [Meme] 正在迁移旧版 memory.txt ...", flush=True)
            try:
                with open(self.old_memory_file,'r',encoding='utf-8') as f: c = f.read()
                # 兼容旧格式切分
                parts = c.split("---") if "---" in c else [c]
                for p in parts:
                    if p.strip(): 
                        kw = jieba.analyse.extract_tags(p,topK=5) if HAS_JIEBA else []
                        self.fragments.append({"content":p.strip()[:300],"keywords":kw,"time":"Legacy"})
                self.save_json(self.fragments_file, self.fragments)
                os.rename(self.old_memory_file, self.old_memory_file+".bak")
                print("✅ [Meme] 迁移完成", flush=True)
            except: pass

    # 邮件备份
    async def _backup_daemon(self):
        while self.running:
            now = datetime.datetime.now()
            if now.hour == 4 and now.minute == 0: await self._perform_backup(True); await asyncio.sleep(70)
            await asyncio.sleep(30)
    
    async def _perform_backup(self, auto=False):
        cfg = self.local_config
        if not cfg.get("email_host") or not cfg.get("email_user"): return "No Email Config"
        try:
            b = io.BytesIO()
            with zipfile.ZipFile(b,'w',zipfile.ZIP_DEFLATED) as z:
                z.writestr("sticky_notes.json", json.dumps(self.sticky_notes, ensure_ascii=False))
                z.writestr("memory_fragments.json", json.dumps(self.fragments, ensure_ascii=False))
                z.writestr("memes.json", json.dumps(self.meme_data, ensure_ascii=False))
                z.writestr("config.json", json.dumps(self.local_config, ensure_ascii=False))
            def _send():
                msg = MIMEMultipart()
                msg['From'] = cfg['email_user']; msg['To'] = cfg.get('backup_receiver', cfg['email_user'])
                msg['Subject'] = f"[MemeBackup] {datetime.date.today()}"
                msg.attach(MIMEApplication(b.getvalue(), Name="backup.zip"))
                with smtplib.SMTP_SSL(cfg['email_host'], int(cfg.get('email_port', 465))) as s:
                    s.login(cfg['email_user'], cfg['email_pass'])
                    s.send_message(msg)
            await asyncio.get_running_loop().run_in_executor(self.executor, _send)
            print("✅ [Meme] 邮件备份成功", flush=True)
            return "Success"
        except Exception as e: 
            print(f"❌ [Meme] 备份失败: {e}", flush=True)
            return str(e)

    # 防抖逻辑 (V2)
    async def _handle_debounce(self, event, uid, msg_str, img_urls):
        db = float(self.local_config.get("debounce_time", 3.0))
        if db <= 0: return False
        if uid in self.sessions:
            s = self.sessions[uid]
            if msg_str: s['queue'].append({'type':'text','content':msg_str})
            for u in img_urls: s['queue'].append({'type':'image','url':u})
            if s.get('timer'): s['timer'].cancel()
            s['timer'] = asyncio.create_task(self._debounce_timer(uid, db))
            event.stop_event()
            return True
        flush = asyncio.Event()
        timer = asyncio.create_task(self._debounce_timer(uid, db))
        q = []
        if msg_str: q.append({'type':'text','content':msg_str})
        for u in img_urls: q.append({'type':'image','url':u})
        self.sessions[uid] = {'queue':q, 'flush':flush, 'timer':timer}
        print(f"⏳ [Meme] 启动防抖...", flush=True)
        await flush.wait()
        if uid not in self.sessions: return False
        s = self.sessions.pop(uid)
        txt = " ".join([i['content'] for i in s['queue'] if i['type']=='text'])
        imgs = [i['url'] for i in s['queue'] if i['type']=='image']
        event.message_str = txt
        setattr(event, "_meme_debounced_imgs", imgs)
        await self.handle_message(event)
        return True

    async def _debounce_timer(self, uid, t):
        try: await asyncio.sleep(t); self.sessions[uid]['flush'].set() if uid in self.sessions else None
        except: pass

    # 自动存图
    def _trigger_auto_save(self, urls, ctx):
        cd = float(self.local_config.get("auto_save_cooldown", 60))
        for u in urls: asyncio.create_task(self.ai_evaluate_image(u, ctx))

    async def ai_evaluate_image(self, url, ctx):
        try:
            d = await self._download_image(url)
            if not d: return
            h = await self._calc_hash_async(d)
            if self._check_hash_exist(h): return
            p = self.context.get_using_provider()
            if not p: return
            res = await p.text_chat(f"适合做表情包吗? YES回: YES|Desc:描述|Keywords:关键词... NO回NO.\nCtx:{ctx}", session_id=None, image_urls=[url])
            t = (getattr(res,"completion_text",None) or getattr(res,"text","")).strip()
            if "YES" in t:
                desc = t.split("Desc:")[-1].split("|")[0].strip() if "Desc:" in t else "Auto"
                kw = t.split("Keywords:")[-1].strip().split() if "Keywords:" in t else []
                c, ext = await self._compress_image(d)
                fn = f"{int(time.time())}{ext}"
                with open(os.path.join(self.img_dir, fn),"wb") as f: f.write(c)
                self.meme_data[fn] = {"tags":desc, "keywords":kw, "source":"auto", "hash":h}
                self.save_json(self.meme_index_file, self.meme_data)
                print(f"🖤 [自动进货] 入库: {desc}", flush=True)
        except: pass

    # WebUI (Backend Only Mode)
    async def start_web_server(self):
        app = web.Application()
        app.router.add_get("/", lambda r: web.Response(text="MemeMaster Backend Running..."))
        # 保留 API 接口供测试
        app.router.add_get("/api/get_data", self.h_get_data)
        runner = web.AppRunner(app)
        await runner.setup()
        port = self.local_config.get("web_port", 5000)
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()

    async def h_get_data(self, r):
        if r.query.get("token") != self.local_config.get("web_token"): return web.Response(status=403)
        return web.json_response({"status": "ok", "frags": len(self.fragments), "memes": len(self.meme_data)})

    # 工具函数
    def load_json(self, path, default_type="dict"):
        try: 
            with open(path,'r',encoding='utf-8') as f: return json.load(f)
        except: return {} if default_type=="dict" else []
    def save_json(self, path, data):
        with open(path,'w',encoding='utf-8') as f: json.dump(data, f, ensure_ascii=False, indent=2)
    def load_config(self):
        d = {"web_port":5000, "debounce_time":3.0, "auto_save_cooldown":60}
        try: d.update(json.load(open(self.config_file))) 
        except: pass
        return d
    def save_config(self): self.save_json(self.config_file, self.local_config)
    def check_config_reload(self): pass
    def get_full_time_str(self):
        now = datetime.datetime.now(); s = now.strftime('%Y-%m-%d %H:%M')
        if HAS_LUNAR: 
            try: l=Solar.fromYmdHms(now.year,now.month,now.day,now.hour,now.minute,now.second).getLunar(); s+=f" (农历{l.getMonthInChinese()}月{l.getDayInChinese()})"
            except: pass
        return s
    def _get_all_img_urls(self, e):
        if hasattr(e, "_meme_debounced_imgs"): return getattr(e, "_meme_debounced_imgs")
        urls = []
        if not e.message_obj or not e.message_obj.message: return urls
        for c in e.message_obj.message:
            if isinstance(c, Image): urls.append(c.url)
        return urls
    def _extract_text_from_result(self, result):
        text = ""
        if isinstance(result, list):
            for c in result:
                if isinstance(c, Plain): text += c.text
        elif hasattr(result, "chain"):
            for c in result.chain:
                if isinstance(c, Plain): text += c.text
        else: text = str(result)
        return text
    async def _download_image(self, url):
        async with aiohttp.ClientSession() as s:
            async with s.get(url) as r: return await r.read() if r.status==200 else None
    async def _calc_hash_async(self, d):
        def _s():
            try:
                i=PILImage.open(io.BytesIO(d)).resize((9,8),PILImage.Resampling.LANCZOS).convert('L'); p=list(i.getdata())
                return hex(sum(2**x for x,v in enumerate([p[r*9+c]>p[r*9+c+1] for r in range(8) for c in range(8)]) if v))[2:]
            except: return None
        return await asyncio.get_running_loop().run_in_executor(self.executor, _s)
    def _check_hash_exist(self, h):
        if not h: return False
        for _,e in self.img_hashes.items():
            if bin(int(h,16)^int(e,16)).count('1')<=5: return True
        return False
    async def _compress_image(self, d):
        def _s():
            try:
                i=PILImage.open(io.BytesIO(d))
                if getattr(i,'is_animated',False): return d, ".gif"
                if i.width>400: i=i.resize((400,int(i.height*(400/i.width))), PILImage.Resampling.LANCZOS)
                b=io.BytesIO(); i.convert("RGB").save(b,"JPEG",quality=75); return b.getvalue(), ".jpg"
            except: return d, ".jpg"
        return await asyncio.get_running_loop().run_in_executor(self.executor, _s)
    async def _init_image_hashes(self):
        for f in os.listdir(self.img_dir):
            if f in self.meme_data and self.meme_data[f].get('hash'): self.img_hashes[f]=self.meme_data[f]['hash']
    def find_best_match(self, q):
        b,s=None,0
        for f,i in self.meme_data.items():
            if q in i.get('tags',''): return os.path.join(self.img_dir,f)
            r=difflib.SequenceMatcher(None,q,i.get('tags','').split(':')[0]).ratio()
            if r>s: s=r; b=f
        return os.path.join(self.img_dir,b) if s>0.4 else None
