import subprocess
import sys
import importlib

DEPENDENCIES = ["jieba", "lunar_python", "Pillow"]

def install_dependencies():
    for pkg in DEPENDENCIES:
        import_name = pkg.replace("-", "_")
        try:
            importlib.import_module(import_name)
        except ImportError:
            print(f"检测到缺少依赖 {pkg}，正在尝试自动安装...", flush=True)
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install", pkg])
                print(f"依赖 {pkg} 安装成功！", flush=True)
            except Exception as e:
                print(f"依赖 {pkg} 安装失败: {e}", flush=True)

install_dependencies()

import jieba
import os
import json
import random
import asyncio
import time
import re
import aiohttp
import difflib
import zipfile
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
import sqlite3
import io
import datetime
from concurrent.futures import ThreadPoolExecutor
from aiohttp import web
from PIL import Image as PILImage

try:
    from lunar_python import Solar
    HAS_LUNAR = True
except ImportError:
    HAS_LUNAR = False

from astrbot.api.star import Context, Star, register
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.event.filter import EventMessageType
from astrbot.core.message.components import Image, Plain

print(">>> [Meme] 插件主文件 v23 (Logic Perfected) 已被系统加载 <<<", flush=True)

@register("vv_meme_master", "Vvivloy", "防抖/图库/记忆", "3.0.0")
class MemeMaster(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.base_dir = os.path.abspath(os.path.dirname(__file__))
        self.img_dir = os.path.join(self.base_dir, "images")
        self.data_file = os.path.join(self.base_dir, "memes.json")
        self.config_file = os.path.join(self.base_dir, "config.json")
        self.memory_file = os.path.join(self.base_dir, "memory.txt") 
        self.buffer_file = os.path.join(self.base_dir, "buffer.json") 
        self.init_db()  # <--- 就加这一行
        
        self.executor = ThreadPoolExecutor(max_workers=4)
        self.running = True
        
        if not os.path.exists(self.img_dir): os.makedirs(self.img_dir, exist_ok=True)
            
        self.local_config = self.load_config()
        if "web_token" not in self.local_config:
            self.local_config["web_token"] = "admin123"
            self.save_config()

        self.data = self.load_data()
        self.chat_history_buffer = self.load_buffer_from_disk()
        self.current_summary = self.load_memory()
        self.img_hashes = {} 
        self.sessions = {} 
        self.msg_count = 0 
        
        self.is_summarizing = False
        self.last_auto_save_time = 0
        self.last_active_time = time.time()
        
        self.pair_map = {'“': '”', '《': '》', '（': '）', '(': ')', '"': '"', "'": "'"}
        self.split_chars = "\n。？！?!"

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.start_web_server())
            loop.create_task(self._init_image_hashes())
            loop.create_task(self._lonely_watcher())
            print("✅ [Meme] 核心服务启动成功！", flush=True)
        except Exception as e:
            print(f"❌ [Meme] 服务启动失败: {e}", flush=True)

    # === 动作2：把这段代码复制到 MemeMaster 类里面 ===
    def init_db(self):
        """初始化 SQLite 数据库结构"""
        db_path = os.path.join(self.base_dir, "meme_core.db")
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        
        # 1. 表情表：存图片信息
        c.execute('''CREATE TABLE IF NOT EXISTS memes (
            filename TEXT PRIMARY KEY,
            tags TEXT,
            feature_hash TEXT,
            source TEXT DEFAULT 'manual',
            created_at REAL,
            last_used REAL DEFAULT 0,
            usage_count INTEGER DEFAULT 0
        )''')
        
        # 2. 记忆表：存对话、总结和小纸条
        c.execute('''CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            type TEXT DEFAULT 'fragment', 
            keywords TEXT,
            importance INTEGER DEFAULT 1,
            created_at REAL
        )''')
        
        # 3. 访问日志：记录互动时间
        c.execute('''CREATE TABLE IF NOT EXISTS access_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            action_type TEXT,
            timestamp REAL
        )''')
        
        # 4. 配置表：存设置
        c.execute('''CREATE TABLE IF NOT EXISTS system_config (
            key TEXT PRIMARY KEY,
            value TEXT
        )''')

        conn.commit()
        conn.close()
        print("✅ [Meme] 数据库完整性检查通过", flush=True)

    # === 动作：把这段代码贴在 init_db 函数后面 ===
    # 注意：def 和上面的 def init_db 必须保持左边对齐！
    
    def merge_legacy_data(self, legacy_memes=None, legacy_memory="", legacy_buffer=None):
        """
        核心迁移逻辑：把旧的 JSON/TXT 数据无缝融入 SQLite 数据库
        """
        try:
            db_path = os.path.join(self.base_dir, "meme_core.db")
            conn = sqlite3.connect(db_path)
            c = conn.cursor()
            count_meme = 0
            count_mem = 0

            # 1. 搬运表情包 (memes.json)
            if legacy_memes:
                for fn, info in legacy_memes.items():
                    try:
                        # INSERT OR IGNORE: 如果文件已存在，就自动忽略，保护现有数据
                        c.execute('''INSERT OR IGNORE INTO memes 
                                   (filename, tags, source, created_at, feature_hash) 
                                   VALUES (?, ?, ?, ?, ?)''', 
                                  (fn, info.get('tags', '未分类'), info.get('source', 'manual'), 
                                   time.time(), info.get('hash', '')))
                        if c.rowcount > 0:
                            count_meme += 1
                    except: pass

            # 2. 搬运长期记忆 (memory.txt)
            if legacy_memory and legacy_memory.strip():
                # type='sticky' 代表这是一条至关重要的置顶记忆 (权重10)
                c.execute('''INSERT INTO memories (content, type, importance, created_at) 
                             VALUES (?, 'sticky', 10, ?)''', 
                          (legacy_memory, time.time()))
                count_mem += 1

            # 3. 搬运短时缓存 (buffer.json)
            if legacy_buffer and isinstance(legacy_buffer, list):
                for msg in legacy_buffer:
                    # type='dialogue' 代表这是普通的对话流水 (权重1)
                    c.execute('''INSERT INTO memories (content, type, importance, created_at) 
                                 VALUES (?, 'dialogue', 1, ?)''', 
                              (str(msg), time.time()))
                count_mem += 1

            conn.commit()
            conn.close()
            print(f"📦 [Meme] 数据迁移完成: 新增表情 {count_meme} 张, 记忆片段 {count_mem} 条", flush=True)
            return True, f"成功导入 {count_meme} 张表情，{count_mem} 条记忆"
            
        except Exception as e:
            print(f"❌ [Meme] 数据迁移失败: {e}", flush=True)
            return False, str(e)

    # === 替换原来的 get_db_context ===
    def get_db_context(self, current_query=""):
        """
        全能读取器：
        1. 必读：核心规则 (Sticky)
        2. 联想：根据你现在说的话，去搜相关的旧片段 (Fragment)
        3. 补全：最近的对话流水 (Dialogue)
        """
        try:
            conn = sqlite3.connect(os.path.join(self.base_dir, "meme_core.db"))
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            
            # 1. 取出核心规则 (永远置顶)
            c.execute("SELECT content FROM memories WHERE type='sticky' ORDER BY importance DESC")
            stickies = [row['content'] for row in c.fetchall()]

            related_memories = []
            
            # 2. 联想召回
            related_memories = []
            if current_query:
                # 使用 jieba 进行搜索引擎模式分词，并过滤掉单字（停用词）
                query_words = [w for w in jieba.cut_for_search(current_query) if len(w) > 1]
    
                if query_words:
                    conditions = []
                    params = []
                    for w in query_words:
                        conditions.append("(keywords LIKE ? OR content LIKE ?)")
                        params.extend([f"%{w}%", f"%{w}%"])
        
                    if conditions:
                        # 限制只找最相关的 3 条
                        sql = f"SELECT content FROM memories WHERE type='fragment' AND ({' OR '.join(conditions)}) ORDER BY created_at DESC LIMIT 3"
                        c.execute(sql, tuple(params))
                        related_memories = [row['content'] for row in c.fetchall()]

            # 3. 兜底：如果没联想到，就拿最近生成的 2 条碎片看看
            c.execute("SELECT content FROM memories WHERE type='fragment' ORDER BY created_at DESC LIMIT 2")
            recent_memories = [row['content'] for row in c.fetchall()]
            
            # 4. 关键：取出最近 15 条对话 (填补短期记忆空白)
            c.execute("SELECT content FROM memories WHERE type='dialogue' ORDER BY created_at DESC LIMIT 15")
            dialogues = [row['content'] for row in c.fetchall()][::-1] # 反转回正序

            conn.close()
            
            # 去重合并
            final_fragments = list(set(related_memories + recent_memories))

            context_list = []
            if stickies: 
                context_list.append("【绝对规则/核心设定】\n" + "\n".join(stickies))
            if final_fragments: 
                context_list.append("【相关回忆/背景】\n" + "\n".join(final_fragments))
            if dialogues: 
                context_list.append("【最近的对话】\n" + "\n".join(dialogues))
                
            return "\n\n".join(context_list).strip()
        except Exception as e:
            print(f"❌ 读取记忆出错: {e}")
            return ""

    def __del__(self):
        self.running = False 

    async def _debounce_timer(self, uid: str, duration: float):
        try:
            await asyncio.sleep(duration)
            if uid in self.sessions: 
                self.sessions[uid]['flush_event'].set()
        except asyncio.CancelledError: pass

    @filter.event_message_type(EventMessageType.PRIVATE_MESSAGE, priority=1)
    async def handle_private(self, event: AstrMessageEvent):
        await self._master_handler(event)

    @filter.event_message_type(EventMessageType.GROUP_MESSAGE, priority=1)
    async def handle_group(self, event: AstrMessageEvent):
        await self._master_handler(event)

    # ==========================
    # 主逻辑
    # ==========================
    async def _master_handler(self, event: AstrMessageEvent):
        # 1. 基础防爆 & 自检
        try:
            user_id = str(event.message_obj.sender.user_id)
            bot_id = None
            if hasattr(self.context, 'get_current_provider_bot'):
                bot = self.context.get_current_provider_bot()
                if bot: bot_id = str(bot.self_id)
            if bot_id and user_id == bot_id: return
        except: pass

        try:
            self.check_config_reload()

            msg_str = (event.message_str or "").strip()
            uid = event.unified_msg_origin
            img_urls = self._get_all_img_urls(event)

            # ★★★ 核心修复1：空消息安检门 ★★★
            # 这里的 not msg_str 是判断有没有文字
            # 这里的 not img_urls 是判断有没有图片
            # 如果都没有，说明这只是 NapCat 发来的“正在输入”状态通知，直接扔掉！
            if not msg_str and not img_urls:
                return 
            
            # 收到有效消息日志
            print(f"📨 [Meme] 收到: {msg_str[:10]}... (图:{len(img_urls)})", flush=True)

            self.last_active_time = time.time()
            self.last_uid = uid
            self.last_session_id = event.session_id

            # 自动进货
            if img_urls and not msg_str.startswith("/"):
                cd = float(self.local_config.get("auto_save_cooldown", 60))
                if time.time() - self.last_auto_save_time > cd:
                    self.last_auto_save_time = time.time()
                    for url in img_urls:
                        asyncio.create_task(self.ai_evaluate_image(url, msg_str))

            # 指令穿透
            if (msg_str.startswith("/") or msg_str.startswith("！") or msg_str.startswith("!")) and not img_urls:
                if uid in self.sessions:
                    if self.sessions[uid].get('timer_task'): self.sessions[uid]['timer_task'].cancel()
                    self.sessions[uid]['flush_event'].set()
                return 

            # 防抖逻辑
            try: debounce_time = float(self.local_config.get("debounce_time", 3.0))
            except: debounce_time = 3.0

            if debounce_time > 0:
                if uid in self.sessions:
                    s = self.sessions[uid]
                    if msg_str: s['queue'].append({'type': 'text', 'content': msg_str})
                    for url in img_urls: s['queue'].append({'type': 'image', 'url': url})
                    
                    if s.get('timer_task'): s['timer_task'].cancel()
                    s['timer_task'] = asyncio.create_task(self._debounce_timer(uid, debounce_time))
                    
                    event.stop_event()
                    print(f"⏳ [Meme] 防抖追加 (Q:{len(s['queue'])})", flush=True)
                    return 

                print(f"🆕 [Meme] 启动防抖 ({debounce_time}s)...", flush=True)
                flush_event = asyncio.Event()
                timer_task = asyncio.create_task(self._debounce_timer(uid, debounce_time))
                
                initial_queue = []
                if msg_str: initial_queue.append({'type': 'text', 'content': msg_str})
                for url in img_urls: initial_queue.append({'type': 'image', 'url': url})

                self.sessions[uid] = {
                    'queue': initial_queue, 'flush_event': flush_event, 'timer_task': timer_task
                }
                
                await flush_event.wait()
                
                if uid not in self.sessions: return 
                s = self.sessions.pop(uid)
                queue = s['queue']
                if not queue: return

                combined_text_list = []
                combined_images = []
                for item in queue:
                    if item['type'] == 'text': combined_text_list.append(item['content'])
                    elif item['type'] == 'image': combined_images.append(item['url'])
                
                msg_str = " ".join(combined_text_list)
                img_urls = combined_images

            # 记忆处理
            self.msg_count += 1
            threshold = self.local_config.get("summary_threshold", 40)
            curr_len = len(self.chat_history_buffer)
            print(f"📊 [Meme] 处理完毕 (记忆: {curr_len}/{threshold})", flush=True)

            img_mark = f" [Image*{len(img_urls)}]" if img_urls else ""
            log_entry = f"User: {msg_str}{img_mark}"
            self.chat_history_buffer.append(log_entry)
            self.save_buffer_to_disk()
            
            time_info = self.get_full_time_str()
            system_context = [f"Time: {time_info}"]
            
            # === [新记忆调用逻辑] ===
            # 获取注入间隔（默认20条消息触发一次深度记忆注入，防止Prompt过长）
            mem_interval = int(self.local_config.get("memory_interval", 20))
            injected_mem = False
            
            # 只有满足间隔条件，或者这是第一条消息时，才去翻数据库
            if mem_interval > 0 and (self.msg_count == 1 or self.msg_count % mem_interval == 0):
                # 调用你刚才加的“全能读取器”，把当前用户说的话 (msg_str) 传进去联想
                db_mem = self.get_db_context(msg_str) 
                
                if db_mem:
                    # 这里的标签可以起得正式一点，告诉 AI 这是它的记忆系统
                    system_context.append(f"Internal Memory System:\n{db_mem}")
                    injected_mem = True

            hints = []
            if random.randint(1, 100) <= int(self.local_config.get("reply_prob", 50)):
                all_tags = [v.get("tags", "").split(":")[0].strip() for v in self.data.values()]
                if all_tags:
                    hints = random.sample(all_tags, min(15, len(all_tags)))
                    hint_str = " ".join([f"<MEME:{h}>" for h in hints])
                    system_context.append(f"Meme Hints: {hint_str}")

            # 日志会告诉你这次有没有注入记忆
            mem_status = "✅已注入" if injected_mem else "⏭️跳过"
            print(f"💉 [Meme] 上下文: 时间={time_info} | 记忆={mem_status} | 表情={len(hints)}", flush=True)

            final_text = f"{msg_str}\n\n(System Context: {' | '.join(system_context)})"
            
            event.message_str = final_text
            chain = [Plain(final_text)]
            for url in img_urls: chain.append(Image.fromURL(url))
            event.message_obj.message = chain
            
        except Exception as e:
            import traceback
            print(f"❌ [Meme] 主逻辑严重错误: {e}", flush=True)
            traceback.print_exc()

    @filter.on_decorating_result(priority=0)
    async def on_output(self, event: AstrMessageEvent):
        if getattr(event, "__meme_processed", False): return
        
        result = event.get_result()
        if not result: return
        
        text = ""
        if isinstance(result, list):
            for c in result:
                if isinstance(c, Plain): text += c.text
        elif hasattr(result, "chain"):
            for c in result.chain:
                if isinstance(c, Plain): text += c.text
        else: text = str(result)
            
        if not text: return
        setattr(event, "__meme_processed", True)
        
        text = self.clean_markdown(text)
        
        clean_text_for_log = re.sub(r"\(System Context:.*?\)", "", text).strip()
        self.chat_history_buffer.append(f"AI: {clean_text_for_log}")
        self.save_buffer_to_disk()
        
        if not self.is_summarizing:
            asyncio.create_task(self.check_and_summarize())

        try:
            pattern = r"(<MEME:.*?>|MEME_TAG:\s*[\S]+)"
            parts = re.split(pattern, text)
            mixed_chain = []
            has_meme = False
            
            for part in parts:
                tag = None
                if part.startswith("<MEME:"): tag = part[6:-1].strip()
                elif "MEME_TAG:" in part: tag = part.replace("MEME_TAG:", "").strip()
                
                if tag:
                    path = self.find_best_match(tag)
                    if path: 
                        print(f"🎯 [Meme] 命中表情包: [{tag}]", flush=True)
                        mixed_chain.append(Image.fromFileSystem(path))
                        has_meme = True
                elif part:
                    clean_part = part.replace("(System Context:", "").replace(")", "").strip()
                    if clean_part: mixed_chain.append(Plain(clean_part))
            
            if not has_meme and len(text) < 50 and "\n" not in text: return

            segments = self.smart_split(mixed_chain)
            delay_base = self.local_config.get("delay_base", 0.5)
            delay_factor = self.local_config.get("delay_factor", 0.1)
            
            for i, seg in enumerate(segments):
                txt_len = sum(len(c.text) for c in seg if isinstance(c, Plain))
                wait = delay_base + (txt_len * delay_factor)
                
                mc = MessageChain()
                mc.chain = seg
                await self.context.send_message(event.unified_msg_origin, mc)
                if i < len(segments) - 1: await asyncio.sleep(wait)
            
            event.set_result(None) 

        except Exception as e:
            print(f"❌ [Meme] 输出处理出错: {e}", flush=True)

    def clean_markdown(self, text):
        text = re.sub(r"(?si)[\s\.]*thought.*?End of thought", "", text)
        text = re.sub(r"<thought>.*?</thought>", "", text, flags=re.DOTALL)
        text = text.replace("**", "")
        text = text.replace("### ", "").replace("## ", "")
        if text.startswith("> "): text = text[2:]
        return text.strip()

    def smart_split(self, chain):
        segs = []; buf = []
        def flush(): 
            if buf: segs.append(buf[:]); buf.clear()
        
        for c in chain:
            if isinstance(c, Image): flush(); segs.append([c]); continue
            if isinstance(c, Plain):
                txt = c.text; idx = 0; chunk = ""; stack = []
                while idx < len(txt):
                    char = txt[idx]
                    if char in self.pair_map: stack.append(char)
                    elif stack and char == self.pair_map[stack[-1]]: stack.pop()
                    
                    is_split_char = char in self.split_chars
                    force_split = (len(chunk) > 80)
                    
                    if (not stack and is_split_char) or force_split:
                        chunk += char
                        if is_split_char:
                            while idx + 1 < len(txt) and txt[idx+1] in self.split_chars: 
                                idx += 1; chunk += txt[idx]
                        if chunk.strip(): buf.append(Plain(chunk))
                        flush(); chunk = ""
                    else: chunk += char
                    idx += 1
                if chunk: buf.append(Plain(chunk))
        flush(); return segs
    
    # ... 下面是 Config/Data/Server 部分 ...

    def load_config(self):
        default = {
            "web_port": 5000, "debounce_time": 3.0, "reply_prob": 50, 
            "auto_save_cooldown": 60, "memory_interval": 20, 
            "summary_threshold": 40, "proactive_interval": 0,
            "quiet_start": 23, "quiet_end": 7,
            "delay_base": 0.5, "delay_factor": 0.1,
            "web_token": "admin123", # 确保有默认token
            "ai_prompt": "判断这张图是否适合做表情包。适合回YES并给出<名称>:说明，不适合回NO。",
            "smtp_host": "", "smtp_user": "", "smtp_pass": "", "email_to": "" # 默认设置为空字符串
        }
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    content = json.load(f)
                    default.update(content)
            except: pass
        return default

    def check_config_reload(self):
        if os.path.exists(self.config_file):
            try:
                mtime = os.path.getmtime(self.config_file)
                if mtime > self.config_mtime:
                    print(f"🔄 [Meme] 配置文件热重载", flush=True)
                    self.local_config = self.load_config()
            except: pass
            
    async def check_and_summarize(self):
        """自动消化系统 v3.0：确保总结成功才删除缓存，失败则持续累积"""
        threshold = self.local_config.get("summary_threshold", 40)
        # 如果还没到条数，或者正在总结中，就不动
        if len(self.chat_history_buffer) < threshold or self.is_summarizing: 
            return
        
        self.is_summarizing = True 
        try:
            print(f"🧠 [Meme] 正在尝试总结记忆 (当前积压: {len(self.chat_history_buffer)} 条)...", flush=True)
            now_str = self.get_full_time_str()
            history_text = "\n".join(self.chat_history_buffer)
            
            provider = self.context.get_using_provider()
            if not provider: 
                self.is_summarizing = False
                return
            
            prompt = f"""
            Task: Analyze the conversation for Long-term Memory.
            Current Time: {now_str}
            
            Output a JSON object with 3 fields:
            1. "summary" (Required): A concise summary of the conversation flow (under 200 words).
            2. "keywords" (Required): Comma-separated keywords for search.
            3. "sticky_content" (Optional): 
               - ONLY if the user explicitly defined a PERMANENT RULE, STRONG PREFERENCE, or IMPORTANT FACT (e.g., "Call me Baby", "My birthday is 5/20", "Never eat spicy food").
               - If found, extract it as a short, absolute statement.
               - If nothing critical found, leave this field empty string "".
            
            Conversation:
            {history_text}
            """
            
            resp = await provider.text_chat(prompt, session_id=None)
            raw_text = (getattr(resp, "completion_text", None) or getattr(resp, "text", "")).strip()
            
            # 解析结果
            summary, keywords, sticky = "", "", ""
            try:
                clean_json = raw_text.replace("```json", "").replace("```", "").strip()
                data = json.loads(clean_json)
                summary = f"[{now_str}] {data.get('summary', '')}"
                keywords = data.get('keywords', '')
                sticky = data.get('sticky_content', '').strip()
            except:
                # 兜底：如果AI没回JSON，就把全文当总结
                summary = f"[{now_str}] {raw_text[:200]}"
                keywords = "history"

            # 写入数据库
            conn = sqlite3.connect(os.path.join(self.base_dir, "meme_core.db"))
            c = conn.cursor()
            if summary:
                c.execute("INSERT INTO memories (content, type, keywords, importance, created_at) VALUES (?, 'fragment', ?, 5, ?)", 
                          (summary, keywords, time.time()))
            if sticky:
                c.execute("SELECT id FROM memories WHERE type='sticky' AND content=?", (sticky,))
                if not c.fetchone():
                    c.execute("INSERT INTO memories (content, type, importance, created_at) VALUES (?, 'sticky', 10, ?)", 
                              (sticky, time.time()))
            conn.commit()
            conn.close()
            
            # 【关键】只有走到这一步（成功写入DB），才清空 buffer
            self.chat_history_buffer = [] 
            self.save_buffer_to_disk()
            print(f"✨ [Meme] 记忆消化完成，已清空缓存。", flush=True)

        except Exception as e:
            print(f"❌ [Meme] 消化失败 (保留缓存待下次重试): {e}", flush=True)
        finally:
            self.is_summarizing = False
            
    async def ai_evaluate_image(self, img_url, context_text=""):
        try:
            img_data = None
            async with aiohttp.ClientSession() as s:
                async with s.get(img_url) as r:
                    if r.status == 200: img_data = await r.read()
            if not img_data: return

            current_hash = await self._calc_hash_async(img_data)
            if current_hash:
                for _, exist_hash in self.img_hashes.items():
                    if bin(int(current_hash, 16) ^ int(exist_hash, 16)).count('1') <= 5:
                        print(f"♻️ [自动进货] 图片已存在 (指纹匹配)，跳过", flush=True)
                        return

            provider = self.context.get_using_provider()
            if not provider: return
            
            raw_prompt = self.local_config.get("ai_prompt", "")
            prompt = raw_prompt.replace("{context_text}", context_text) if "{context_text}" in raw_prompt else raw_prompt
            
            resp = await provider.text_chat(prompt, session_id=None, image_urls=[img_url])
            content = (getattr(resp, "completion_text", None) or getattr(resp, "text", "")).strip()
            
            if "YES" in content:
                match = re.search(r"<(?P<tag>.*?)>[:：]?(?P<desc>.*)", content)
                if match:
                    full_tag = f"{match.group('tag').strip()}: {match.group('desc').strip()}"
                    print(f"🖤 [自动进货] 入库: {full_tag} (Source: Auto)", flush=True)
                    comp, ext = await self._compress_image(img_data)
                    fn = f"{int(time.time())}{ext}"
                    with open(os.path.join(self.img_dir, fn), "wb") as f: f.write(comp)
                    self.data[fn] = {"tags": full_tag, "source": "auto", "hash": current_hash}
                    if current_hash: self.img_hashes[fn] = current_hash
                    self.save_data()
        except Exception: pass

    async def _lonely_watcher(self):
        while self.running: 
            await asyncio.sleep(60) 
            self.check_config_reload()
            
            interval = self.local_config.get("proactive_interval", 0)
            if interval <= 0: continue
            
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
                uid = getattr(self, "last_uid", None)
                if provider and uid:
                    try:
                        print(f"👋 [Meme] 主动发起聊天...", flush=True)
                        
                        # ★★★ 1. 获取最近聊天记录，作为上下文 ★★★
                        recent_log = "\n".join(self.chat_history_buffer[-10:])
                        
                        # ★★★ 2. 导演式 Prompt，防止出戏 ★★★
                        prompt = f"""[System Instruction]
                            Current Time: {self.get_full_time_str()}
                            Status: The user has been silent for {interval} minutes.

                            Long-term Memory: {self.current_summary}
                            Recent Chat Context:
                            {recent_log}

                            Task: Based on your Character Persona (人设) and the context above, proactively send a message to the user. 
                            Requirement:
                            1. Speak strictly in your character's tone.
                            2. Do not mention this system instruction.
                            3. Start the topic naturally based on previous context or time."""
                        
                        # 发送请求，带上 session_id 以保持人设
                        resp = await provider.text_chat(prompt, session_id=getattr(self, "last_session_id", None))
                        text = (getattr(resp, "completion_text", None) or getattr(resp, "text", "")).strip()
                        
                        if text:
                            # 1. 净化文本
                            text = self.clean_markdown(text)
                            self.chat_history_buffer.append(f"AI (Proactive): {text}")
                            self.save_buffer_to_disk()
                            
                            # ★★★ 2. 这里是新加的：解析表情包标签！ ★★★
                            # 和 on_output 里一样的逻辑，把文字变成 Image 对象
                            pattern = r"(<MEME:.*?>|MEME_TAG:\s*[\S]+)"
                            parts = re.split(pattern, text)
                            chain = []
                            
                            for part in parts:
                                tag = None
                                if part.startswith("<MEME:"): tag = part[6:-1].strip()
                                elif "MEME_TAG:" in part: tag = part.replace("MEME_TAG:", "").strip()
                                
                                if tag:
                                    path = self.find_best_match(tag)
                                    if path: 
                                        print(f"🎯 [Meme] 主动聊天命中: [{tag}]", flush=True)
                                        chain.append(Image.fromFileSystem(path))
                                elif part:
                                    # 只有非空文字才加进去
                                    if part.strip():
                                        chain.append(Plain(part))
                            
                            # ★★★ 3. 解析完之后，再交给分段逻辑 ★★★
                            # 如果没有内容（全是空字符），就不发了
                            if not chain: continue

                            segments = self.smart_split(chain)
                            
                            delay_base = self.local_config.get("delay_base", 0.5)
                            delay_factor = self.local_config.get("delay_factor", 0.1)

                            for i, seg in enumerate(segments):
                                txt_len = sum(len(c.text) for c in seg if isinstance(c, Plain))
                                wait = delay_base + (txt_len * delay_factor)
                                
                                mc = MessageChain()
                                mc.chain = seg
                                await self.context.send_message(uid, mc)
                                if i < len(segments) - 1: await asyncio.sleep(wait)

                    except Exception as e:
                        print(f"❌ [Meme] 主动聊天出错: {e}", flush=True)

    async def _init_image_hashes(self):
        if not os.path.exists(self.img_dir): return
        count = 0
        for f in os.listdir(self.img_dir):
            if not f.lower().endswith(('.jpg', '.png', '.jpeg', '.gif', '.webp')): continue
            if f in self.data and 'hash' in self.data[f] and self.data[f]['hash']:
                self.img_hashes[f] = self.data[f]['hash']
                continue
            try:
                path = os.path.join(self.img_dir, f)
                with open(path, "rb") as fl: content = fl.read()
                h = await self._calc_hash_async(content)
                if h: 
                    self.img_hashes[f] = h
                    if f not in self.data: self.data[f] = {"tags": "未分类", "source": "unknown"}
                    self.data[f]['hash'] = h
                    count += 1
            except: pass
        self.save_data()
        print(f"✅ [Meme] 指纹库加载完毕，有效图片: {len(self.img_hashes)}", flush=True)

    async def _calc_hash_async(self, image_data):
        def _sync():
            try:
                img = PILImage.open(io.BytesIO(image_data))
                if getattr(img, 'is_animated', False): img.seek(0)
                img = img.resize((9, 8), PILImage.Resampling.LANCZOS).convert('L')
                pixels = list(img.getdata())
                val = sum(2**i for i, v in enumerate([pixels[row*9+col] > pixels[row*9+col+1] for row in range(8) for col in range(8)]) if v)
                return hex(val)[2:]
            except: return None
        return await asyncio.get_running_loop().run_in_executor(self.executor, _sync)

    async def _compress_image(self, image_data: bytes):
        def _sync():
            try:
                img = PILImage.open(io.BytesIO(image_data))
                if getattr(img, 'is_animated', False): return image_data, ".gif"
                max_w = 400
                if img.width > max_w:
                    ratio = max_w / img.width
                    img = img.resize((max_w, int(img.height * ratio)), PILImage.Resampling.LANCZOS)
                buf = io.BytesIO()
                if img.mode != "RGB": img = img.convert("RGB")
                img.save(buf, format="JPEG", quality=75)
                return buf.getvalue(), ".jpg"
            except: return image_data, ".jpg"
        return await asyncio.get_running_loop().run_in_executor(self.executor, _sync)

    def _get_all_img_urls(self, e):
        urls = []
        if not e.message_obj or not e.message_obj.message: return urls
        for c in e.message_obj.message:
            if isinstance(c, Image): urls.append(c.url)
        return urls

    def find_best_match(self, query):
        best, score = None, 0
        for f, i in self.data.items():
            t = i.get("tags", "")
            if query in t: return os.path.join(self.img_dir, f)
            s = difflib.SequenceMatcher(None, query, t.split(":")[0]).ratio()
            if s > score: score = s; best = f
        if score > 0.4: return os.path.join(self.img_dir, best)
        return None
    
    def save_config(self): 
        try: json.dump(self.local_config, open(self.config_file,"w"), indent=2)
        except: pass
    def load_data(self): return json.load(open(self.data_file)) if os.path.exists(self.data_file) else {}
    def save_data(self): json.dump(self.data, open(self.data_file,"w"), ensure_ascii=False)
    def load_buffer_from_disk(self):
        try: return json.load(open(self.buffer_file, "r"))
        except: return []
    def save_buffer_to_disk(self):
        try: json.dump(self.chat_history_buffer, open(self.buffer_file, "w"), ensure_ascii=False)
        except: pass
    def load_memory(self):
        try: return open(self.memory_file, "r", encoding="utf-8").read()
        except: return ""
    def read_file(self, n): 
        try: return open(os.path.join(self.base_dir, n), "r", encoding="utf-8").read()
        except: return ""
    def check_auth(self, r): return r.query.get("token") == self.local_config.get("web_token")

    def get_full_time_str(self):
        now = datetime.datetime.now()
        time_str = now.strftime('%Y-%m-%d %H:%M')
        if HAS_LUNAR:
            try:
                lunar = Solar.fromYmdHms(now.year, now.month, now.day, now.hour, now.minute, now.second).getLunar()
                time_str += f" (农历{lunar.getMonthInChinese()}月{lunar.getDayInChinese()})"
            except: pass
        return time_str

    async def start_web_server(self):
        app = web.Application()
        app._client_max_size = 100 * 1024 * 1024 
        app.router.add_get("/", self.h_idx)
        app.router.add_post("/upload", self.h_up)
        app.router.add_post("/batch_delete", self.h_del)
        app.router.add_post("/update_tag", self.h_tag)
        app.router.add_get("/get_config", self.h_gcf)
        app.router.add_post("/update_config", self.h_ucf)
        app.router.add_get("/backup", self.h_backup)
        app.router.add_post("/restore", self.h_restore)
        app.router.add_post("/slim_images", self.h_slim)
        app.router.add_post("/test_email", self.h_test_email)
        app.router.add_post("/import_legacy", self.h_import_legacy) # <--- 新加的
        app.router.add_get("/get_stickies", self.h_get_stickies) # <--- 新加
        app.router.add_post("/update_sticky", self.h_update_sticky) # <--- 新加
        app.router.add_static("/images/", path=self.img_dir)
        runner = web.AppRunner(app)
        await runner.setup()
        port = self.local_config.get("web_port", 5000)
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        print(f"🌐 [Meme] WebUI 管理后台已启动: http://localhost:{port}", flush=True)

    async def h_idx(self,r): 
        if not self.check_auth(r): return web.Response(status=403, text="Need ?token=xxx")
        token = self.local_config["web_token"]
        html = self.read_file("index.html").replace("{{MEME_DATA}}", json.dumps(self.data)).replace("admin123", token)
        return web.Response(text=html, content_type="text/html")
    async def h_up(self, r):
        if not self.check_auth(r): return web.Response(status=403)
        rd = await r.multipart(); tag="未分类"
        while True:
            p = await rd.next()
            if not p: break
            if p.name == "tags": tag = await p.text()
            elif p.name == "file":
                raw = await p.read()
                comp, ext = await self._compress_image(raw)
                fn = f"{int(time.time()*1000)}_{random.randint(100,999)}{ext}"
                with open(os.path.join(self.img_dir, fn), "wb") as f: f.write(comp)
                h = await self._calc_hash_async(comp) 
                self.data[fn] = {"tags": tag, "source": "manual", "hash": h}
                if h: self.img_hashes[fn] = h
        self.save_data(); return web.Response(text="ok")
    async def h_del(self,r):
        if not self.check_auth(r): return web.Response(status=403)
        for f in (await r.json()).get("filenames",[]):
            try: os.remove(os.path.join(self.img_dir,f)); del self.data[f]; self.img_hashes.pop(f, None)
            except: pass
        self.save_data(); return web.Response(text="ok")
    async def h_tag(self,r):
        if not self.check_auth(r): return web.Response(status=403)
        d=await r.json(); self.data[d['filename']]['tags']=d['tags']; self.save_data(); return web.Response(text="ok")
    async def h_gcf(self,r): return web.json_response(self.local_config)
    async def h_gcf(self,r): 
        return web.json_response(self.local_config)

    async def h_ucf(self, r):
        if not self.check_auth(r): return web.Response(status=403)
        try:
            new_conf = await r.json()
            for k, v in new_conf.items():
                if k in ['web_token', 'ai_prompt', 'smtp_host', 'smtp_user', 'smtp_pass', 'email_to']:
                    # 关键修复：如果 v 是 None 或者 字符串 "None"，就存为空字符串
                    val = str(v) if v is not None else ""
                    if val.lower() == "none": val = ""
                    self.local_config[k] = val
                else:
                    try:
                        if v is not None and str(v).strip() != "" and str(v).lower() != "none":
                            self.local_config[k] = float(v)
                    except: pass
            self.save_config()
            return web.Response(text="ok")
        except Exception as e:
            return web.Response(status=500, text=str(e))

    async def h_backup(self,r):
        if not self.check_auth(r): return web.Response(status=403)
        b=io.BytesIO()
        with zipfile.ZipFile(b,'w',zipfile.ZIP_DEFLATED) as z:
            for root,_,files in os.walk(self.img_dir): 
                for f in files: z.write(os.path.join(root,f),f"images/{f}")
            if os.path.exists(self.data_file): z.write(self.data_file,"memes.json")
            if os.path.exists(self.config_file): z.write(self.config_file,"config.json")
            if os.path.exists(self.buffer_file): z.write(self.buffer_file, "buffer.json")
            db_p = os.path.join(self.base_dir, "meme_core.db")
            if os.path.exists(db_p): z.write(db_p, "meme_core.db")
        b.seek(0)
        return web.Response(body=b, headers={'Content-Disposition':'attachment; filename="meme_backup.zip"'})

    async def h_restore(self, r):
        if not self.check_auth(r): return web.Response(status=403)
        try:
            reader = await r.multipart()
            field = await reader.next()
            file_data = await field.read()
            def unzip_action():
                with zipfile.ZipFile(io.BytesIO(file_data), 'r') as z: 
                    z.extractall(self.base_dir)
            await asyncio.get_running_loop().run_in_executor(self.executor, unzip_action)
            # 重新加载数据
            self.data = self.load_data()
            self.local_config = self.load_config()
            self.chat_history_buffer = self.load_buffer_from_disk()
            return web.Response(text="ok")
        except Exception as e:
            return web.Response(status=500, text=str(e))

    # === 动作1：贴在文件底部的 Web 处理区域 ===
    async def h_import_legacy(self, r):
        """WebUI 接口：接收旧数据文件并导入"""
        if not self.check_auth(r): return web.Response(status=403, text="Forbidden")
        try:
            reader = await r.multipart()
            memes_data, memory_text, buffer_data = None, "", []

            # 循环读取上传的每一个文件
            while True:
                field = await reader.next()
                if not field: break
                
                # 读取并解码
                content = await field.read()
                if not content: continue
                
                if field.name == 'memes_json':
                    try: memes_data = json.loads(content.decode('utf-8'))
                    except: pass
                elif field.name == 'memory_txt':
                    memory_text = content.decode('utf-8')
                elif field.name == 'buffer_json':
                    try: buffer_data = json.loads(content.decode('utf-8'))
                    except: pass

            # 呼叫搬运工
            success, msg = self.merge_legacy_data(memes_data, memory_text, buffer_data)
            return web.Response(text=msg if success else "Error: " + msg)
            
        except Exception as e:
            return web.Response(status=500, text=f"Server Error: {str(e)}")

    # === 动作 1: 获取核心记忆列表 ===
    async def h_get_stickies(self, r):
        if not self.check_auth(r): return web.Response(status=403)
        conn = sqlite3.connect(os.path.join(self.base_dir, "meme_core.db"))
        conn.row_factory = sqlite3.Row # 让我们能用字段名取数据
        c = conn.cursor()
        # 只取 sticky 类型的记忆，按时间倒序
        c.execute("SELECT id, content, created_at FROM memories WHERE type='sticky' ORDER BY created_at DESC")
        rows = [dict(ix) for ix in c.fetchall()]
        conn.close()
        return web.json_response(rows)

    # === 动作 2: 增删改核心记忆 ===
    async def h_update_sticky(self, r):
        if not self.check_auth(r): return web.Response(status=403)
        data = await r.json()
        action = data.get('action') # add / delete / edit
        
        conn = sqlite3.connect(os.path.join(self.base_dir, "meme_core.db"))
        c = conn.cursor()
        
        try:
            if action == 'add':
                content = data.get('content', '').strip()
                if content:
                    # 插入一条 sticky 记忆，权重设为 10 (最高)
                    c.execute("INSERT INTO memories (content, type, importance, created_at) VALUES (?, 'sticky', 10, ?)", 
                             (content, time.time()))
            
            elif action == 'delete':
                mid = data.get('id')
                if mid:
                    c.execute("DELETE FROM memories WHERE id=? AND type='sticky'", (mid,))
            
            elif action == 'edit':
                mid = data.get('id')
                content = data.get('content', '').strip()
                if mid and content:
                    c.execute("UPDATE memories SET content=? WHERE id=? AND type='sticky'", (content, mid))
            
            conn.commit()
            return web.Response(text="ok")
        except Exception as e:
            return web.Response(status=500, text=str(e))
        finally:
            conn.close()
            
    async def h_slim(self, r):
        if not self.check_auth(r): return web.Response(status=403)
        loop = asyncio.get_running_loop()
        count = 0
        for f in os.listdir(self.img_dir):
            try:
                p = os.path.join(self.img_dir, f)
                with open(p, 'rb') as fl: raw = fl.read()
                nd, _ = await self._compress_image(raw)
                if len(nd) < len(raw):
                    with open(p, 'wb') as fl: fl.write(nd)
                    count += 1
            except: pass
        return web.Response(text=f"优化了 {count} 张")
        
    async def h_test_email(self, r):
        if not self.check_auth(r): return web.Response(status=403)
        res = await self.send_backup_email()
        return web.Response(text=res)

    async def send_backup_email(self):
        conf = self.local_config
        host = conf.get("smtp_host")
        user = conf.get("smtp_user")
        pw = conf.get("smtp_pass")
        to_email = conf.get("email_to")
        
        if not all([host, user, pw, to_email]): return "配置不全：请检查SMTP主机、账号、授权码和收件人"

        try:
            # 创建内存中的压缩包（不包含图片，只包含核心数据库和配置，防止邮件太大）
            zip_data = io.BytesIO()
            with zipfile.ZipFile(zip_data, 'w', zipfile.ZIP_DEFLATED) as z:
                db_path = os.path.join(self.base_dir, "meme_core.db")
                if os.path.exists(db_path): z.write(db_path, "meme_core.db")
                if os.path.exists(self.data_file): z.write(self.data_file, "memes.json")
                if os.path.exists(self.config_file): z.write(self.config_file, "config.json")
            
            msg = MIMEMultipart()
            msg['Subject'] = f"MemeMaster 备份 - {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}"
            msg['From'] = user
            msg['To'] = to_email
            
            part = MIMEBase('application', "octet-stream")
            part.set_payload(zip_data.getvalue())
            encoders.encode_base64(part)
            part.add_header('Content-Disposition', 'attachment; filename="meme_core_backup.zip"')
            msg.attach(part)
            
            def _send():
                # 使用 SSL 连接 SMTP
                with smtplib.SMTP_SSL(host, 465) as server:
                    server.login(user, pw)
                    server.send_message(msg)
            
            await asyncio.get_running_loop().run_in_executor(self.executor, _send)
            return "✅ 备份邮件已发送，请查收"
        except Exception as e:
            return f"❌ 发送失败: {str(e)}"
