import subprocess
import sys
import importlib

DEPENDENCIES = ["jieba", "lunar_python", "Pillow","aiohttp"]

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
import jieba.analyse
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
from email.mime.text import MIMEText
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
from astrbot.api.event import filter, AstrMessageEvent, MessageChain, MessageEventResult
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
        self.init_db()

        self.db_lock = asyncio.Lock()          # 数据库写入锁 (防 locked)
        self.api_semaphore = asyncio.Semaphore(1) # 鉴图并发锁 (防 429)
        
        self.executor = ThreadPoolExecutor(max_workers=4)
        self.running = True
        
        if not os.path.exists(self.img_dir): os.makedirs(self.img_dir, exist_ok=True)
            
        self.local_config = self.load_config()
        if "web_token" not in self.local_config:
            self.local_config["web_token"] = "admin123"
            self.save_config()

        self.data = self.load_data()
        # 多 bot: buffer 改为 {bot_id: [...]} 字典
        self.chat_history_buffers = self.load_buffer_from_disk()
        self.current_summary = self.load_memory()
        self.img_hashes = {}
        # 多 bot: sessions/typing 都按 bot_id 隔离；session key = f"{bot_id}::{uid}"
        self.sessions = {}
        self._typing_times = {}        # key: (bot_id, user_id) -> ts
        # round_count 和 sticky_updated 从 DB 读取（且按 bot_id 隔离），不再用实例变量
        self.pending_user_msg = ""
        self.rounds_since_sticky = 0
        self.config_mtime = os.path.getmtime(self.config_file) if os.path.exists(self.config_file) else 0

        # 多 bot: 总结/进货冷却/活跃时间 都按 bot_id 隔离
        self.summarizing_bots = set()      # 正在总结的 bot_id 集合
        self.last_auto_save_times = {}     # bot_id -> ts
        self.last_active_times = {}        # bot_id -> ts
        self.last_uids = {}                # bot_id -> uid
        self.last_session_ids = {}         # bot_id -> session_id
        self.last_email_date = ""

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

    DEFAULT_BOT_ID = "default"

    # === v3.0: 配置作用域分类 ===
    # 全局配置 (config.json)：所有 bot 共享；不在数据库 bot_configs 表里
    GLOBAL_CONFIG_KEYS = {
        "web_token", "web_port",
        "smtp_host", "smtp_user", "smtp_pass", "email_to",
        "target_bot_id",
    }
    # Bot 级配置 (bot_configs 表)：每个 bot 独立；如未设置则回退 local_config 同名 key
    BOT_CONFIG_KEYS = {
        "ai_prompt",
        "debounce_time", "typing_debounce",
        "delay_base", "delay_factor",
        "ab_context_rounds",
        "proactive_interval", "quiet_start", "quiet_end",
        "auto_save_cooldown", "meme_match_threshold",
    }

    def init_db(self):
        """初始化 SQLite 数据库 (v3.0 多 Bot 支持)"""
        db_path = os.path.join(self.base_dir, "meme_core.db")
        conn = sqlite3.connect(db_path)
        c = conn.cursor()

        # 1. 表情表
        c.execute('''CREATE TABLE IF NOT EXISTS memes (
            filename TEXT PRIMARY KEY,
            tags TEXT,
            feature_hash TEXT,
            source TEXT DEFAULT 'manual',
            created_at REAL,
            last_used REAL DEFAULT 0,
            usage_count INTEGER DEFAULT 0
        )''')
        c.execute("CREATE INDEX IF NOT EXISTS idx_memes_tags ON memes(tags);")

        # 2. 记忆表
        c.execute('''CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            keywords TEXT,
            type TEXT DEFAULT 'dialogue',
            importance INTEGER DEFAULT 1,
            created_at REAL
        )''')
        c.execute("CREATE INDEX IF NOT EXISTS idx_memories_keywords ON memories(keywords);")
        c.execute("CREATE INDEX IF NOT EXISTS idx_memories_content ON memories(content);")
        c.execute("CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(type);")

        # 3. 访问日志 & 配置表
        c.execute('''CREATE TABLE IF NOT EXISTS access_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, action_type TEXT, timestamp REAL)''')
        c.execute('''CREATE TABLE IF NOT EXISTS system_config (key TEXT PRIMARY KEY, value TEXT)''')

        # 4. Bots 表 (v3.0 新增)
        c.execute('''CREATE TABLE IF NOT EXISTS bots (
            bot_id TEXT PRIMARY KEY,
            nickname TEXT,
            created_at REAL,
            last_active REAL
        )''')

        # 5. Bot 配置/状态表 (v3.0 新增)
        c.execute('''CREATE TABLE IF NOT EXISTS bot_configs (
            bot_id TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT,
            PRIMARY KEY (bot_id, key)
        )''')

        # === Schema 迁移：给 memes 和 memories 加 bot_id 列 ===
        c.execute("PRAGMA table_info(memes)")
        meme_cols = [row[1] for row in c.fetchall()]
        if "bot_id" not in meme_cols:
            c.execute("ALTER TABLE memes ADD COLUMN bot_id TEXT")
            c.execute("CREATE INDEX IF NOT EXISTS idx_memes_bot ON memes(bot_id);")
            print("📦 [Meme] memes 表已升级 (新增 bot_id 列)", flush=True)

        c.execute("PRAGMA table_info(memories)")
        memo_cols = [row[1] for row in c.fetchall()]
        if "bot_id" not in memo_cols:
            c.execute("ALTER TABLE memories ADD COLUMN bot_id TEXT")
            c.execute("CREATE INDEX IF NOT EXISTS idx_memories_bot ON memories(bot_id);")
            print("📦 [Meme] memories 表已升级 (新增 bot_id 列)", flush=True)

        # === 一次性迁移：把所有 bot_id=NULL 的旧数据归入 default bot ===
        # (幂等：用 system_config 打标记，恢复旧备份后也能重跑)
        c.execute("SELECT value FROM system_config WHERE key='null_to_default_migrated'")
        migrated = c.fetchone()
        if not migrated:
            n_memes = c.execute(f"UPDATE memes SET bot_id = ? WHERE bot_id IS NULL", (self.DEFAULT_BOT_ID,)).rowcount
            n_memos = c.execute(f"UPDATE memories SET bot_id = ? WHERE bot_id IS NULL", (self.DEFAULT_BOT_ID,)).rowcount
            c.execute("INSERT OR REPLACE INTO system_config (key, value) VALUES ('null_to_default_migrated', '1')")
            if n_memes or n_memos:
                print(f"📦 [Meme] 旧数据已归入 default bot: {n_memes} 张图 + {n_memos} 条记忆", flush=True)

        # 确保 default bot 始终存在 (兜底)
        c.execute("INSERT OR IGNORE INTO bots (bot_id, nickname, created_at, last_active) VALUES (?, ?, ?, ?)",
                  (self.DEFAULT_BOT_ID, "默认 Bot", time.time(), time.time()))

        conn.commit()
        conn.close()
        print("✅ [Meme] 数据库 v3.0 初始化完成 (多 Bot 支持已就绪)", flush=True)

    def _db_path(self):
        return os.path.join(self.base_dir, "meme_core.db")

    def _get_config_val(self, key, default="0"):
        """从 system_config 表读取状态值"""
        try:
            conn = sqlite3.connect(self._db_path(), timeout=5)
            try:
                c = conn.cursor()
                c.execute("SELECT value FROM system_config WHERE key=?", (key,))
                row = c.fetchone()
                return row[0] if row else default
            finally:
                conn.close()
        except:
            return default

    def _set_config_val(self, key, value):
        """写入 system_config 表"""
        try:
            conn = sqlite3.connect(self._db_path(), timeout=5)
            try:
                c = conn.cursor()
                c.execute("INSERT OR REPLACE INTO system_config (key, value) VALUES (?, ?)", (key, str(value)))
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            print(f"❌ [Config] 写入失败 key={key}: {e}", flush=True)

    # ==========================
    # Bot 注册 / 配置 (v3.0)
    # ==========================
    def list_bots(self):
        """列出所有已注册的 bot"""
        try:
            conn = sqlite3.connect(self._db_path(), timeout=5)
            try:
                conn.row_factory = sqlite3.Row
                c = conn.cursor()
                c.execute("SELECT bot_id, nickname, created_at, last_active FROM bots ORDER BY created_at ASC")
                return [dict(r) for r in c.fetchall()]
            finally:
                conn.close()
        except Exception:
            return []

    def register_bot(self, bot_id: str, nickname: str = ""):
        """注册或更新 bot 信息 (幂等)"""
        if not bot_id: return
        bot_id = str(bot_id)
        nickname = nickname or bot_id
        try:
            conn = sqlite3.connect(self._db_path(), timeout=5)
            try:
                c = conn.cursor()
                now = time.time()
                # 已存在则只更新昵称和活跃时间，不覆盖 created_at
                c.execute("INSERT OR IGNORE INTO bots (bot_id, nickname, created_at, last_active) VALUES (?, ?, ?, ?)",
                          (bot_id, nickname, now, now))
                c.execute("UPDATE bots SET nickname=?, last_active=? WHERE bot_id=?",
                          (nickname, now, bot_id))
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            print(f"❌ [Bot] 注册失败 {bot_id}: {e}", flush=True)

    def get_bot_config(self, bot_id: str, key: str, default=""):
        """读取某个 bot 的某项配置 (优先 bot_configs，回退 local_config 全局值)"""
        if not bot_id: bot_id = self.DEFAULT_BOT_ID
        try:
            conn = sqlite3.connect(self._db_path(), timeout=5)
            try:
                c = conn.cursor()
                c.execute("SELECT value FROM bot_configs WHERE bot_id=? AND key=?", (bot_id, key))
                row = c.fetchone()
                if row is not None:
                    return row[0]
            finally:
                conn.close()
        except Exception:
            pass
        # 回退到全局 local_config 中的值（兼容旧逻辑）
        return self.local_config.get(key, default)

    def set_bot_config(self, bot_id: str, key: str, value):
        """写入某个 bot 的某项配置"""
        if not bot_id: bot_id = self.DEFAULT_BOT_ID
        try:
            conn = sqlite3.connect(self._db_path(), timeout=5)
            try:
                c = conn.cursor()
                c.execute("INSERT OR REPLACE INTO bot_configs (bot_id, key, value) VALUES (?, ?, ?)",
                          (bot_id, key, str(value) if value is not None else ""))
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            print(f"❌ [Bot Config] 写入失败 {bot_id}.{key}: {e}", flush=True)

    def get_all_bot_config(self, bot_id: str):
        """读取某个 bot 的所有配置 (返回 dict)，只含 bot_configs 里实际存了的"""
        if not bot_id: bot_id = self.DEFAULT_BOT_ID
        result = {}
        try:
            conn = sqlite3.connect(self._db_path(), timeout=5)
            try:
                c = conn.cursor()
                c.execute("SELECT key, value FROM bot_configs WHERE bot_id=?", (bot_id,))
                for k, v in c.fetchall():
                    result[k] = v
            finally:
                conn.close()
        except Exception:
            pass
        return result

    def get_effective_bot_config(self, bot_id: str):
        """返回某 bot 的"有效"配置 (默认值 ← local_config ← bot_configs 三层覆盖)
        只包含 BOT_CONFIG_KEYS 列出的字段。"""
        if not bot_id: bot_id = self.DEFAULT_BOT_ID
        overrides = self.get_all_bot_config(bot_id)
        result = {}
        for k in self.BOT_CONFIG_KEYS:
            if k in overrides:
                result[k] = overrides[k]
            elif k in self.local_config:
                result[k] = self.local_config[k]
        return result


    def extract_keywords(self, text):
        """本地离线分词 (0成本)"""
        if not text: return ""
        # 提取前10个关键词，允许名词(n)、动词(v)、人名(nr)等
        tags = jieba.analyse.extract_tags(text, topK=10, allowPOS=('n', 'nr', 'ns', 'nt', 'nz', 'v', 'vn'))
        return ",".join(tags)

    async def save_message_to_db(self, content, msg_type='dialogue', bot_id=None):
        """v24: 异步锁 + 报错屏蔽。v3.0: 加 bot_id"""
        if not content: return
        if "AstrBot 请求失败" in content or "请在平台日志查看" in content:
            return
        bot_id = bot_id or self.DEFAULT_BOT_ID
        async with self.db_lock:
            try:
                kw = self.extract_keywords(content)
                conn = sqlite3.connect(self._db_path(), timeout=10)
                c = conn.cursor()
                c.execute("INSERT INTO memories (content, keywords, type, created_at, bot_id) VALUES (?, ?, ?, ?, ?)",
                          (content, kw, msg_type, time.time(), bot_id))
                conn.commit()
                conn.close()
            except sqlite3.OperationalError:
                pass
            except Exception as e:
                print(f"❌ 存库小错误: {e}", flush=True)

    def get_related_context(self, current_text, bot_id=None):
        """智能检索：找 Sticky + 找相关回忆 (0成本)。v3.0: 按 bot_id 隔离"""
        bot_id = bot_id or self.DEFAULT_BOT_ID
        conn = sqlite3.connect(self._db_path())
        try:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()

            context_list = []

            # 1. 必读：Sticky 核心规则 (永远置顶)
            c.execute("SELECT content FROM memories WHERE type='sticky' AND bot_id=? ORDER BY created_at DESC", (bot_id,))
            stickies = [f"【核心设定/重要事实】 {row['content']}" for row in c.fetchall()]
            if stickies: context_list.extend(stickies)

            # 2. 选读：根据当前那句话，去搜相关的旧记忆
            if current_text:
                search_kws = list(jieba.cut_for_search(current_text))
                search_kws = [w for w in search_kws if len(w) > 1]

                if search_kws:
                    conditions = []
                    params = []
                    for w in search_kws:
                        conditions.append("(keywords LIKE ? OR content LIKE ?)")
                        params.extend([f"%{w}%", f"%{w}%"])

                    if conditions:
                        sql = f"SELECT content, created_at FROM memories WHERE bot_id=? AND type IN ('dialogue', 'fragment') AND ({' OR '.join(conditions)}) ORDER BY created_at DESC LIMIT 3"
                        c.execute(sql, (bot_id, *params))
                        related = [f"【相关回忆】 {row['content']}" for row in c.fetchall()]
                        if related: context_list.extend(related)

            return "\n".join(context_list)
        finally:
            conn.close()
        
    def get_meme_candidates(self, current_text, bot_id=None):
        """v24: 情绪反转 + 关键词混合检索 (让AI做选择)。v3.0: 全局 + bot 自有"""
        bot_id = bot_id or self.DEFAULT_BOT_ID
        if not current_text: return []
        
        # 1. 情绪反转字典 (用户哭 -> 搜安慰)
        mood_map = {
            '负面': {'keywords': ['难过', '哭', '累', '死', '痛', '委屈', '烦', '不开心'], 'search': ['安慰', '抱抱', '摸摸', '贴贴', '爱你']},
            '正面': {'keywords': ['开心', '哈哈', '笑死', '耶', '棒'], 'search': ['震惊', '庆祝', '无语', '疑惑']}
        }
        
        search_terms = set()
        
        # 2. 也是用 jieba 分词
        user_kws = list(jieba.cut(current_text))
        
        # 3. 命中情绪词？强制加入反转词
        hit_mood = False
        for k in user_kws:
            for m_type, m_data in mood_map.items():
                if k in m_data['keywords']:
                    search_terms.update(m_data['search'])
                    hit_mood = True
        
        # 4. 如果没命中强情绪，或者为了多样性，也加上原文关键词
        search_terms.update([w for w in user_kws if len(w) > 1])
        
        if not search_terms: return []

        # 5. 查库 (v3.0: 全局 NULL ∪ bot 自有)
        conn = sqlite3.connect(self._db_path())
        c = conn.cursor()
        candidates = []

        try:
            for term in list(search_terms):
                c.execute("SELECT tags FROM memes WHERE tags LIKE ? AND (bot_id IS NULL OR bot_id=?) ORDER BY usage_count DESC LIMIT 3",
                          (f"%{term}%", bot_id))
                for row in c.fetchall():
                    candidates.append(row[0])
            # === 保底机制：如果搜出来的太少，随机补货 ===
            if len(candidates) < 2:
                c.execute("SELECT tags FROM memes WHERE (bot_id IS NULL OR bot_id=?) ORDER BY RANDOM() LIMIT 3", (bot_id,))
                for row in c.fetchall():
                    candidates.append(row[0])
        except: pass
        finally: conn.close()
        
        # 6. 随机打散并取前 6 个，给 AI 更多选择空间
        final_list = list(set(candidates))
        random.shuffle(final_list)
        return final_list[:6]
    
    def merge_legacy_data(self, legacy_memes=None, legacy_memory="", legacy_buffer=None):
        """将旧 JSON 数据导入 SQLite"""
        try:
            conn = sqlite3.connect(self._db_path())
            c = conn.cursor()
            count = 0
            
            # 1. 导入旧 meme.json (历史手动上传保持全局 NULL，自动进货归入默认 bot)
            if legacy_memes:
                for fn, info in legacy_memes.items():
                    try:
                        src = info.get('source', 'manual')
                        legacy_bot = None if src == 'manual' else self.DEFAULT_BOT_ID
                        c.execute("INSERT OR IGNORE INTO memes (filename, tags, source, feature_hash, created_at, bot_id) VALUES (?, ?, ?, ?, ?, ?)",
                                  (fn, info.get('tags'), src, info.get('hash', ''), time.time(), legacy_bot))
                        count += 1
                    except: pass
            
            # 2. 导入 memory.txt (按段落粒度切割)
            if legacy_memory and legacy_memory.strip():
                current_date = ""
                # 按行遍历，识别日期 + 按空行分段
                paragraphs = []
                current_para = []
                
                for line in legacy_memory.split("\n"):
                    stripped = line.strip()
                    
                    # 识别 C格式日期: --- 2025-12-29 ... ---
                    date_c = re.match(r"---\s*(\d{4}-\d{2}-\d{2}).*---", stripped)
                    # 识别 B格式日期: 2025年12月16日 | 标题
                    date_b = re.match(r"(\d{4}年\d{1,2}月\d{1,2}日)\s*[|｜]", stripped)
                    # 识别编号标题: 1. xxx  2. xxx (A格式大总结)
                    numbered = re.match(r"^\d+\.\s+", stripped)
                    
                    if date_c:
                        # 遇到新日期，先保存之前的段落
                        if current_para:
                            paragraphs.append((current_date, "\n".join(current_para)))
                            current_para = []
                        current_date = date_c.group(1)
                    elif date_b:
                        if current_para:
                            paragraphs.append((current_date, "\n".join(current_para)))
                            current_para = []
                        current_date = date_b.group(1)
                        current_para.append(stripped)  # 标题行也保留
                    elif numbered and len(current_para) > 2:
                        # 编号标题且已有内容，分段
                        paragraphs.append((current_date, "\n".join(current_para)))
                        current_para = [stripped]
                    elif stripped == "":
                        # 空行 = 段落分隔（但要求当前段落至少有内容）
                        if current_para:
                            paragraphs.append((current_date, "\n".join(current_para)))
                            current_para = []
                    else:
                        current_para.append(stripped)
                
                # 别忘了最后一段
                if current_para:
                    paragraphs.append((current_date, "\n".join(current_para)))
                
                # 存入数据库
                for date_str, content in paragraphs:
                    content = content.strip()
                    if not content or len(content) < 5:
                        continue  # 跳过太短的碎片
                    # 有日期就加前缀
                    full_content = f"{date_str} | {content}" if date_str else content
                    kw = self.extract_keywords(full_content)
                    c.execute("INSERT INTO memories (content, keywords, type, created_at, bot_id) VALUES (?, ?, 'fragment', ?, ?)",
                              (full_content, kw, time.time(), self.DEFAULT_BOT_ID))
            # 3. 导入 buffer.json (旧数据归入默认 bot)
            if legacy_buffer and isinstance(legacy_buffer, list):
                for msg in legacy_buffer:
                    msg_str = str(msg).strip()
                    if msg_str and "AstrBot 请求失败" not in msg_str:
                        kw = self.extract_keywords(msg_str)
                        c.execute("INSERT INTO memories (content, keywords, type, created_at, bot_id) VALUES (?, ?, 'dialogue', ?, ?)",
                                  (msg_str, kw, time.time(), self.DEFAULT_BOT_ID))

            conn.commit()
            return True, f"成功导入 {count} 张图片 + 记忆 + 对话记录"
        except Exception as e:
            print(f"❌ [Meme] 数据迁移失败: {e}", flush=True)
            return False, str(e)
        finally:
            try: conn.close()
            except: pass


    def get_db_context(self, current_query="", bot_id=None):
        """v24: 只提取 Sticky + 强相关记忆 (非最近)。v3.0: 按 bot_id 隔离"""
        bot_id = bot_id or self.DEFAULT_BOT_ID
        conn = sqlite3.connect(self._db_path())
        try:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()

            # 1. Sticky: 按冷却策略，这里只取数据，注入逻辑在 handler 里做
            c.execute("SELECT content FROM memories WHERE type='sticky' AND bot_id=?", (bot_id,))
            stickies = [row['content'] for row in c.fetchall()]

            related_fragments = []

            # 2. 相关性检索：用 TF-IDF 提取关键词，评分制匹配
            if current_query:
                query_words = [w for w in jieba.analyse.extract_tags(current_query, topK=5) if len(w) > 1]
                if query_words:
                    score_parts = []
                    params_score = []
                    for w in query_words:
                        score_parts.append("(CASE WHEN keywords LIKE ? OR content LIKE ? THEN 1 ELSE 0 END)")
                        params_score.extend([f"%{w}%", f"%{w}%"])
                    score_expr = " + ".join(score_parts)

                    min_score = 2 if len(query_words) >= 2 else 1
                    # ab_context_rounds 改为 bot 级配置
                    context_window = int(self.get_bot_config(bot_id, "ab_context_rounds", self.local_config.get("ab_context_rounds", 50)))

                    sql_frag = f"SELECT content, ({score_expr}) as score FROM memories WHERE bot_id=? AND type='fragment' AND ({score_expr}) >= {min_score} ORDER BY score DESC, created_at DESC LIMIT 2"
                    c.execute(sql_frag, (*params_score, bot_id, *params_score))
                    related_fragments = [f"【相关总结】{row['content']}" for row in c.fetchall()]

                    sql_dial = f"SELECT content, ({score_expr}) as score FROM memories WHERE bot_id=? AND type='dialogue' AND ({score_expr}) >= {min_score} ORDER BY created_at DESC LIMIT 4 OFFSET {context_window}"
                    c.execute(sql_dial, (*params_score, bot_id, *params_score))
                    related_fragments += [f"【相关对话】{row['content']}" for row in c.fetchall()]

            context_list = []
            if related_fragments:
                context_list.extend(related_fragments)

            return stickies, "\n".join(context_list)
        except Exception as e:
            return [], ""
        finally:
            conn.close()
            
    def __del__(self):
        self.running = False 

    async def _debounce_timer(self, sess_key: str, duration: float, bot_id: str = None, user_id: str = None):
        """防抖计时器: 支持正在输入延长。v3.0: 按 (bot_id, user_id) 查 typing 状态。"""
        try:
            await asyncio.sleep(duration)
            # 智能延长: 仅在启用输入检测时生效 (按 bot 取配置)
            td_val = str(self.get_bot_config(bot_id or self.DEFAULT_BOT_ID, "typing_debounce",
                                             self.local_config.get("typing_debounce", "off"))).strip().lower()
            if td_val in ["on", "1", "true", "开"]:
                typing_times = self._typing_times
                # 兼容老式调用 (没传 bot_id/user_id 时尝试从 sess_key 解析)
                if user_id is None:
                    user_id = sess_key.split(':')[-1] if ':' in sess_key else sess_key
                tk = (bot_id or self.DEFAULT_BOT_ID, str(user_id))
                max_extra_wait = 30
                waited = 0
                while waited < max_extra_wait:
                    last_type = typing_times.get(tk, 0)
                    if time.time() - last_type < 3.0:
                        print(f"⌨️ [Debounce] 用户还在输入，继续等待...", flush=True)
                        await asyncio.sleep(2.0)
                        waited += 2
                    else:
                        break
                # 清理过期的 typing 记录 (超过60秒的)
                now = time.time()
                expired = [k for k, v in typing_times.items() if now - v > 60]
                for k in expired: typing_times.pop(k, None)
            if sess_key in self.sessions:
                self.sessions[sess_key]['flush_event'].set()
        except asyncio.CancelledError:
            pass

    @filter.event_message_type(EventMessageType.PRIVATE_MESSAGE, priority=1)
    async def handle_private(self, event: AstrMessageEvent):
        await self._master_handler(event)

    # 群聊暂不处理（避免刷量和上下文混乱）
    # @filter.event_message_type(EventMessageType.GROUP_MESSAGE, priority=1)
    # async def handle_group(self, event: AstrMessageEvent):
    #     await self._master_handler(event)

    # === 正在输入检测: 延长防抖 ===
    @filter.event_message_type(EventMessageType.ALL, priority=99)
    async def notice_handler(self, event: AstrMessageEvent):
        try:
            raw = getattr(event.message_obj, 'raw_message', None)
            if not isinstance(raw, dict) or raw.get('post_type') != 'notice':
                return
            if raw.get('notice_type') == 'notify' and raw.get('sub_type') == 'input_status':
                bot_id = self._bot_id_from_event(event)
                # 当前 bot 未开启输入检测则跳过
                td_val = str(self.get_bot_config(bot_id, "typing_debounce",
                                                 self.local_config.get("typing_debounce", "off"))).strip().lower()
                if td_val not in ["on", "1", "true", "开"]:
                    return
                uid = str(raw.get('user_id', ''))
                if uid:
                    self._typing_times[(bot_id, uid)] = time.time()
                    print(f"⌨️ [Typing] bot={bot_id} 用户 {uid} 正在输入", flush=True)
        except Exception:
            pass

    # ==========================
    # 主逻辑
    # ==========================
    def _current_bot_info(self, event):
        """从事件中提取当前 bot_id 和 nickname，返回 (bot_id, nickname)。
        提取不到时返回 (DEFAULT_BOT_ID, '默认 Bot')，保证下游永远拿到一个有效 bot_id。"""
        try:
            if hasattr(self.context, 'get_current_provider_bot'):
                bot = self.context.get_current_provider_bot()
                if bot and getattr(bot, 'self_id', None):
                    bot_id = str(bot.self_id)
                    # AstrBot 的 bot 对象可能挂载 nickname/name/self_nickname
                    nickname = (getattr(bot, 'nickname', None)
                                or getattr(bot, 'name', None)
                                or getattr(bot, 'self_nickname', None)
                                or bot_id)
                    return bot_id, str(nickname)
        except Exception:
            pass
        return self.DEFAULT_BOT_ID, "默认 Bot"

    def _bot_id_from_event(self, event):
        """便捷方法：只取 bot_id"""
        return self._current_bot_info(event)[0]

    async def _master_handler(self, event: AstrMessageEvent):
        # 1. 基础防爆 & 自检
        bot_id = self.DEFAULT_BOT_ID
        try:
            user_id = str(event.message_obj.sender.user_id)
            if hasattr(self.context, 'get_current_provider_bot'):
                bot = self.context.get_current_provider_bot()
                if bot and user_id == str(bot.self_id): return

                # 机器人白名单验证：如果配置了目标bot且当前bot不符，跳过
                target_bot = self.local_config.get("target_bot_id", "").strip()
                if target_bot and bot and str(bot.self_id) != target_bot:
                    return

            # 自动注册当前 bot
            bot_id, nickname = self._current_bot_info(event)
            self.register_bot(bot_id, nickname)
            # 把 bot_id 挂在 event 上，方便 on_output 等下游拿
            setattr(event, "__meme_bot_id", bot_id)
        except Exception as e:
            print(f"⚠️ [Meme] bot_id 检测失败: {e}", flush=True)

        try:
            self.check_config_reload()
            msg_str = (event.message_str or "").strip()
            uid = event.unified_msg_origin
            # 多 bot: session key 加 bot_id 前缀，避免不同 bot 撞 key
            sess_key = f"{bot_id}::{uid}"
            img_urls = self._get_all_img_urls(event)

            # 空消息过滤
            if not msg_str and not img_urls: return

            print(f"📨 [Meme] 收到[{bot_id}]: {msg_str[:10]}... (图:{len(img_urls)})", flush=True)
            now_ts = time.time()
            self.last_active_times[bot_id] = now_ts
            self.last_uids[bot_id] = uid
            self.last_session_ids[bot_id] = event.session_id

            # 自动进货 (按 bot 各自维护冷却 + 各自配置)
            if img_urls:
                cd = float(self.get_bot_config(bot_id, "auto_save_cooldown",
                                               self.local_config.get("auto_save_cooldown", 60)))
                last_save = self.last_auto_save_times.get(bot_id, 0)
                if now_ts - last_save > cd:
                    self.last_auto_save_times[bot_id] = now_ts
                    for url in img_urls:
                        # 传入 msg_str 作为上下文，bot_id 决定写入哪个 bot 的图库
                        asyncio.create_task(self.ai_evaluate_image(url, msg_str, bot_id=bot_id))

            # === /撤回 命令：撤回刚刚输入的一句话 ===
            if msg_str in ["/撤回", "/undo"]:
                has_pending = False
                recalled_content = ""
                if sess_key in self.sessions and self.sessions[sess_key]['queue']:
                    has_pending = True
                    # 只移除队列里的最后一条消息
                    last_item = self.sessions[sess_key]['queue'].pop()
                    recalled_content = last_item.get('content', '[图片]') if last_item.get('type') == 'text' else '[图片]'

                    # 如果撤回后队列空了，说明全撤回了，取消计时器和 AI 请求
                    if not self.sessions[sess_key]['queue']:
                        timer = self.sessions[sess_key].get('timer_task')
                        if timer: timer.cancel()
                        self.sessions[sess_key]['flush_event'].set()

                setattr(event, "__meme_skipped", True)
                if has_pending:
                    msg = f"✅ 已撤回: {recalled_content}"
                else:
                    msg = "⚠️ 当前没有正在输入的待发消息可以撤回"
                event.set_result(MessageEventResult().message(msg))
                event.stop_event()
                return

            # 指令穿透: 其他命令消息不走我们的处理流程
            cmd_prefixes = ["/", "！", "!"]
            if msg_str and any(msg_str.startswith(p) for p in cmd_prefixes) and not img_urls:
                if sess_key in self.sessions: self.sessions[sess_key]['flush_event'].set()
                setattr(event, "__meme_skipped", True)
                return

            # "/" 开头兜底
            if msg_str.startswith("/"):
                setattr(event, "__meme_skipped", True)
                return
            try:
                debounce_time = float(self.get_bot_config(bot_id, "debounce_time",
                                                          self.local_config.get("debounce_time", 3.0)))
            except: debounce_time = 3.0
            print(f"🔧 [Meme] 防抖值: {debounce_time}", flush=True)  # ← 加这行


            if debounce_time > 0:
                # 取该用户的真实 user_id 用于 typing 检测匹配
                try:
                    real_uid = str(event.message_obj.sender.user_id)
                except Exception:
                    real_uid = uid

                if sess_key in self.sessions:
                    s = self.sessions[sess_key]
                    if msg_str: s['queue'].append({'type': 'text', 'content': msg_str})
                    for url in img_urls: s['queue'].append({'type': 'image', 'url': url})

                    if s.get('timer_task'): s['timer_task'].cancel()
                    s['timer_task'] = asyncio.create_task(self._debounce_timer(sess_key, debounce_time, bot_id, real_uid))

                    event.stop_event()
                    print(f"⏳ [Meme] 防抖追加 (Q:{len(s['queue'])})", flush=True)
                    return

                print(f"🆕 [Meme] 启动防抖 ({debounce_time}s)...", flush=True)
                flush_event = asyncio.Event()
                timer_task = asyncio.create_task(self._debounce_timer(sess_key, debounce_time, bot_id, real_uid))

                initial_queue = []
                if msg_str: initial_queue.append({'type': 'text', 'content': msg_str})
                for url in img_urls: initial_queue.append({'type': 'image', 'url': url})

                self.sessions[sess_key] = {
                    'queue': initial_queue, 'flush_event': flush_event, 'timer_task': timer_task
                }

                await flush_event.wait()

                if sess_key not in self.sessions:
                    event.stop_event()
                    return
                s = self.sessions.pop(sess_key)
                queue = s['queue']
                if not queue:
                    event.stop_event()
                    return

                combined_text_list = []
                combined_images = []
                for item in queue:
                    if item['type'] == 'text': combined_text_list.append(item['content'])
                    elif item['type'] == 'image': combined_images.append(item['url'])

                msg_str = " ".join(combined_text_list)
                img_urls = combined_images

            # === 核心修改区开始 ===

            # 1. 【防应声虫】：绝对不在这里 save_message_to_db (User)！
            # 我们把用户的话暂存在 event 对象里，传给 on_output
            event.user_text_raw = msg_str
            if img_urls:
                setattr(event, "user_has_image", True)

            # 2. 准备 System Context
            time_info = self.get_full_time_str()

            # 3. 检索记忆 (异步执行，防止阻塞，按 bot_id 隔离)
            stickies, related_context = await asyncio.to_thread(self.get_db_context, msg_str, bot_id)

            # 4. Sticky 注入逻辑（频率由 ab_context_rounds 自动计算）
            ab_rounds = int(self.get_bot_config(bot_id, "ab_context_rounds", self.local_config.get("ab_context_rounds", 50)))
            sticky_freq = ab_rounds if ab_rounds <= 20 else ab_rounds // 2

            # === Sticky 变更检测 (用内容 hash，不依赖 h_update_sticky 写标记) ===
            # 多 bot: round_count / sticky_hash 按 bot_id 隔离
            rc_key = f"round_count_{bot_id}"
            sh_key = f"sticky_hash_{bot_id}"
            round_count = int(self._get_config_val(rc_key, "0"))
            current_hash = str(hash(tuple(sorted(stickies)))) if stickies else "empty"
            stored_hash = self._get_config_val(sh_key, "")
            sticky_changed = (current_hash != stored_hash) and stickies
            should_inject = (round_count % sticky_freq == 0) or sticky_changed
            
            print(f"🔍 [Sticky Debug] round_count={round_count}, sticky_freq={sticky_freq}, "
                  f"stickies数量={len(stickies)}, 条件={should_inject}, "
                  f"内容变更={sticky_changed}, "
                  f"内容={stickies[:2] if stickies else '空'}", flush=True)
            
            # 5. 组装 system_tag (顺序: Sticky → 回忆 → 表情包)
            system_tag = "<system_context>\n"
            
            if should_inject and stickies:
                sticky_str = " ".join([f"({s})" for s in stickies])
                system_tag += f"Important Facts (Established Knowledge): {sticky_str}\n"
                system_tag += "(NOTE: You already KNOW these facts. Do NOT repeat them in your response unless asked.)\n"
                # 更新 hash + 重置计数 (按 bot_id 隔离)
                self._set_config_val(sh_key, current_hash)
                if sticky_changed:
                    self._set_config_val(rc_key, "0")

            if related_context:
                system_tag += f"Historical Context (Recall): {related_context}\n"
                system_tag += "(NOTE: The above 'Historical Context' is for background info ONLY. Do NOT reply to it as if it marks the current conversation state.)\n"

            # 6. 智能检索表情包 (异步执行，按 bot_id 隔离)
            meme_hints = await asyncio.to_thread(self.get_meme_candidates, msg_str, bot_id)
            if meme_hints:
                hints_str = " ".join([f"<MEME:{t}>" for t in meme_hints])
                system_tag += f"Available Memes (copy EXACT tag to use): {hints_str}\n"

            system_tag += f"Time: {time_info}\n"
            system_tag += "</system_context>"

            # === DEBUG 2: 看最终拼出来的 system_tag ===
            print(f"🔍 [Sticky Debug 2] system_tag前200字: {system_tag[:200]}", flush=True)

            # 7. 构造最终文本 (system_context 在前, 用户消息在后)
            final_text = f"{system_tag}\n\n{msg_str}"
            
            event.message_str = final_text
            chain = [Plain(final_text)]
            for url in img_urls: chain.append(Image.fromURL(url))
            event.message_obj.message = chain
        except Exception as e:
            # 屏蔽空消息日志
            if "not defined" not in str(e): # 过滤常见无关报错
                print(f"❌ [Meme] Handle Error: {e}", flush=True)


    @filter.on_decorating_result(priority=0)
    async def on_output(self, event: AstrMessageEvent):
        if getattr(event, "__meme_processed", False): return
        if getattr(event, "__meme_skipped", False): return  # 命令消息跳过

        result = event.get_result()
        if not result: return

        # 优先从 _master_handler 挂上的 bot_id 取，否则现场探测
        bot_id = getattr(event, "__meme_bot_id", None) or self._bot_id_from_event(event)

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

        # === DEBUG 3: 看 on_output 收到的原始文本 ===
        has_meme_tag = "<MEME:" in text or "MEME_TAG:" in text
        print(f"🔍 [Meme Debug][{bot_id}] 有标签={has_meme_tag}, 原文前150字: {text[:150]}", flush=True)

        text = self.clean_markdown(text)

        # 如果 AI 输出了 [[MEM:生日是5.20]]，立刻拦截并存入 Sticky
        mem_match = re.search(r"\[\[MEM:(.*?)\]\]", text)
        if mem_match:
            mem_content = mem_match.group(1).strip()
            if mem_content:
                print(f"📝 [Meme] AI 捕获重要事实: {mem_content}", flush=True)
                # 存为 sticky 类型 (按 bot_id 隔离)
                await self.save_message_to_db(mem_content, 'sticky', bot_id=bot_id)
                self.sticky_updated = True
            # 从回复给用户的文本里删掉这行指令，用户看不到
            text = text.replace(mem_match.group(0), "").strip()

        # 防应声虫：成对存库
        user_raw = getattr(event, "user_text_raw", "")
        if getattr(event, "user_has_image", False):
            user_raw += " [图片]"
        user_raw = user_raw.strip()

        # 清理掉 System Context (兼容旧版和新 XML 版)
        ai_clean = re.sub(r"\(System Context:.*?\)", "", text).strip()
        ai_clean = re.sub(r"<system_context>.*?</system_context>", "", ai_clean, flags=re.DOTALL).strip()
        ai_clean = re.sub(r"<MEME:.*?>", "", ai_clean).strip()  # 也清掉 meme 标签

        if user_raw and ai_clean:
            pair_log = f"User: {user_raw}\nAI: {ai_clean}"
            self._buf(bot_id).append(pair_log)
            self.save_buffer_to_disk()
            await self.save_message_to_db(pair_log, 'dialogue', bot_id=bot_id)
            rc_key = f"round_count_{bot_id}"
            new_rc = int(self._get_config_val(rc_key, "0")) + 1
            self._set_config_val(rc_key, str(new_rc))
            self.rounds_since_sticky += 1


        if bot_id not in self.summarizing_bots:
            asyncio.create_task(self.check_and_summarize(bot_id))

        try:
            # 表情包解析 (兼容反引号包裹)
            pattern = r"(`?<MEME:.*?>`?|MEME_TAG:\s*[\S]+)"
            parts = re.split(pattern, text)
            mixed_chain = []
            has_meme = False
            
            for part in parts:
                tag = None
                # 兼容 `<MEME:xxx>` 和 <MEME:xxx> 两种格式
                clean_part_meme = part.strip("`")
                if clean_part_meme.startswith("<MEME:"): tag = clean_part_meme[6:-1].strip()
                elif "MEME_TAG:" in part: tag = part.replace("MEME_TAG:", "").strip()
                
                if tag:
                    path = self.find_best_match(tag, bot_id=bot_id)
                    if path:
                        print(f"🎯 [Meme] 命中表情包: [{tag}]", flush=True)
                        mixed_chain.append(Image.fromFileSystem(path))
                        has_meme = True
                elif part:
                    clean_part = re.sub(r"\(System Context:.*?\)", "", part).strip()
                    if clean_part: mixed_chain.append(Plain(clean_part))
            
            if not has_meme and len(text) < 50 and "\n" not in text:
                event.set_result(MessageChain([Plain(text)]))
                return


            segments = self.smart_split(mixed_chain)
            delay_base = float(self.get_bot_config(bot_id, "delay_base", self.local_config.get("delay_base", 0.5)))
            delay_factor = float(self.get_bot_config(bot_id, "delay_factor", self.local_config.get("delay_factor", 0.1)))
            
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
        # 去掉 AI 用反引号包裹 MEME 标签的情况: `<MEME:xxx>` → <MEME:xxx>
        text = re.sub(r"`(<MEME:.*?>)`", r"\1", text)
        text = re.sub(r"`(MEME_TAG:\s*\S+)`", r"\1", text)
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
                    force_split = False
                    
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
            "web_port": 5000, "debounce_time": 3.0,
            "auto_save_cooldown": 60, "ab_context_rounds": 50,
            "proactive_interval": 0,
            "quiet_start": 23, "quiet_end": 7,
            "delay_base": 0.5, "delay_factor": 0.1,
            "web_token": "admin123", # 确保有默认token
            "ai_prompt": 
            """你是一个专业的表情包筛选员，正在帮我扩充图库。
            用户发送图片时的配文是：“{context_text}”。(请结合该配文理解，但如果配文在玩梗，请以图片视觉事实为准)
            
            【核心原则：严禁幻觉与乱联想】
            1. 视觉识别必须精准：实事求是，禁止幻觉和过度联想二次元内容！
            2. 黑名单（遇到以下内容直接回复 NO）：
            - 严禁 米哈游/原神/崩坏等 miHoYo 相关内容。
            - 严禁 辱女、性别歧视、黄色暴力或让人不适的烂梗。
            - 普通的系统截图、无关的风景照、纯文字聊天记录。
            
            【判断逻辑】
            - 只有当图片是有趣的、可爱的、或具有情绪表达价值的表情包（如 Chiikawa、线条小狗、Kpop爱豆表情、猫猫狗狗、经典Meme、梗图）时，才保存。
            
            【输出格式】
            如果不保存，仅回复：NO
            如果保存，请严格按以下格式回复（若认不出请直接用一句话描述，省略名称）：
            YES
            <准确的名称>:一句简短自然的各种场景使用说明""",
            "smtp_host": "", "smtp_user": "", "smtp_pass": "", "email_to": "", # 默认设置为空字符串
            "typing_debounce": "off", "target_bot_id": ""
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
                    self.config_mtime = mtime   # ← 加这行
            except: pass
            
    async def check_and_summarize(self, bot_id: str = None):
        """v24: 纯粹的总结逻辑 (只生成 Fragment，不重复提取 Sticky)。v3.0: 按 bot_id 隔离"""
        bot_id = bot_id or self.DEFAULT_BOT_ID
        ab_rounds = int(self.get_bot_config(bot_id, "ab_context_rounds", self.local_config.get("ab_context_rounds", 50)))
        if ab_rounds <= 20:
            threshold = ab_rounds
            summary_words = 150
        elif ab_rounds <= 50:
            threshold = int(ab_rounds * 0.8)
            summary_words = 300
        else:
            threshold = 50
            summary_words = 400
        buf = self._buf(bot_id)
        if len(buf) < threshold or bot_id in self.summarizing_bots:
            return

        self.summarizing_bots.add(bot_id)
        try:
            print(f"🧠 [Meme][{bot_id}] 正在消化记忆 (积压: {len(buf)} 条)...", flush=True)
            now_str = self.get_full_time_str()
            # 从 buffer 提取实际消息日期范围
            first_msg = buf[0] if buf else ""
            date_match = re.search(r'\d{4}-\d{2}-\d{2}', first_msg)
            msg_date = date_match.group() if date_match else now_str.split(' ')[0]
            history_text = "\n".join(buf)

            provider = self.context.get_using_provider()
            if not provider: return

            prompt = f"""当前时间：{now_str}
                这是一段过去的对话记录。请将其总结为简练的"长期记忆"。
                【重要规则】如果对话跨越多天，必须按日期分段总结，格式如：
                [2025-12-29] 发生了xxx
                [2025-12-30] 发生了xxx
                如果同一天则只写一个日期。
                重点记录：用户的喜好、重要事件、双方约定。
                忽略：无意义寒暄、重复表情包指令。
                字数限制：{summary_words}字以内。请确保包含适量的细节关键词以便日后检索。

                对话内容：
                {history_text}"""

            resp = await provider.text_chat(prompt, session_id=None)
            summary = (getattr(resp, "completion_text", None) or getattr(resp, "text", "")).strip()
            # 简单清洗一下
            summary = self.clean_markdown(summary)

            if summary:
                # 按行分段存储，每段独立一条 fragment，方便精准检索
                lines = summary.split("\n")
                for line in lines:
                    line = line.strip()
                    if not line or len(line) < 5:
                        continue
                    # 如果这行没有日期前缀，加上日期
                    if not re.match(r"\[?\d{4}", line):
                        line = f"[{msg_date}] {line}"
                    await self.save_message_to_db(line, 'fragment', bot_id=bot_id)

                # 清空该 bot 的 buffer
                self.chat_history_buffers[bot_id] = []
                self.save_buffer_to_disk()
                print(f"✨ [Meme][{bot_id}] 总结完成，已存入片段库。", flush=True)

        except Exception as e:
            print(f"❌ [Meme][{bot_id}] 总结失败: {e}", flush=True)
        finally:
            self.summarizing_bots.discard(bot_id)
            
    async def ai_evaluate_image(self, img_url, context_text="", bot_id=None):
        """v24 Ultimate: 并发锁 + 上下文注入 + 数据库锁 + 详细报错。v3.0: 自动进货归属 bot_id"""
        bot_id = bot_id or self.DEFAULT_BOT_ID
        # 1. API 通行证：串行执行防 429
        async with self.api_semaphore:
            try:
                # 下载图片
                img_data = None
                async with aiohttp.ClientSession() as s:
                    async with s.get(img_url, timeout=15) as r:
                        if r.status == 200: 
                            img_data = await r.read()
                        else:
                            print(f"⚠️ [自动进货] 图片下载失败 (HTTP {r.status}): {img_url}")
                            return
                if not img_data: return
                
                # 2. 计算指纹并去重 (先快速查一次，无锁)
                current_hash = await self._calc_hash_async(img_data)

                conn = sqlite3.connect(self._db_path())
                try:
                    c = conn.cursor()
                    c.execute("SELECT filename FROM memes WHERE feature_hash = ? AND feature_hash != ''", (current_hash,))
                    exists = c.fetchone()
                finally:
                    conn.close()

                if exists:
                    print(f"♻️ [自动进货] 指纹匹配，跳过重复图")
                    return

                # 3. 准备 AI 鉴图
                provider = self.context.get_using_provider()
                if not provider:
                    print("❌ [自动进货] 错误: 无法获取 AI Provider")
                    return

                raw_prompt = self.get_bot_config(bot_id, "ai_prompt", self.local_config.get("ai_prompt", ""))
                prompt = raw_prompt.replace("{context_text}", context_text) if "{context_text}" in raw_prompt else raw_prompt

                resp = await provider.text_chat(prompt, session_id=None, image_urls=[img_url])
                content = (getattr(resp, "completion_text", None) or getattr(resp, "text", "")).strip()

                # 4. 处理 AI 回复
                if "YES" in content:
                    match = re.search(r"<?(?P<tag>[^>\n:：]+)>?[:：]\s*(?P<desc>.*)", content)
                    if match:
                        full_tag = f"{match.group('tag').strip()}: {match.group('desc').strip()}"
                        print(f"🖤 [自动进货] 识图成功: {full_tag}")

                        comp, ext = await self._compress_image(img_data)
                        fn = f"{int(time.time())}_{random.randint(100,999)}{ext}"
                        with open(os.path.join(self.img_dir, fn), "wb") as f: f.write(comp)

                        # 5. 写入数据库 (加锁，二次查重防竞态)
                        async with self.db_lock:
                            conn = sqlite3.connect(self._db_path())
                            try:
                                c = conn.cursor()
                                c.execute("SELECT filename FROM memes WHERE feature_hash = ? AND feature_hash != ''", (current_hash,))
                                if c.fetchone():
                                    print(f"♻️ [自动进货] 并发去重，跳过")
                                    return
                                c.execute("INSERT INTO memes (filename, tags, source, feature_hash, created_at, bot_id) VALUES (?, ?, 'auto', ?, ?, ?)",
                                          (fn, full_tag, current_hash, time.time(), bot_id))
                                conn.commit()
                            except sqlite3.Error as db_err:
                                print(f"❌ [自动进货] 数据库写入失败: {db_err}")
                            finally:
                                conn.close()
                    else:
                        print(f"⚠️ [自动进货] AI 同意存图但格式解析失败: {content[:50]}...")
                elif "NO" in content:
                    # AI 判定不存，这是正常逻辑，不用打印错误
                    pass
                else:
                    print(f"⚠️ [自动进货] AI 回复内容未包含 YES/NO: {content[:50]}...")

            except asyncio.TimeoutError:
                print(f"❌ [自动进货] 请求超时 (网络问题)")
            except Exception as e:
                # 打印所有未预见的异常
                print(f"❌ [自动进货] 识图任务执行出错: {e}")



    async def _lonely_watcher(self):
        while self.running:
            await asyncio.sleep(60)
            today = datetime.datetime.now().strftime('%Y-%m-%d')
            hour = datetime.datetime.now().hour
            if hour == 5 and today != self.last_email_date:
                if self.local_config.get("smtp_host"):
                    try:
                        await self.send_backup_email()
                        self.last_email_date = today
                        print("📧 [Meme] 每日备份邮件已发送", flush=True)
                    except: pass

            self.check_config_reload()

            # 多 bot: 每个 bot 独立判断主动聊天
            for bot in self.list_bots():
                bot_id = bot["bot_id"]
                interval = float(self.get_bot_config(bot_id, "proactive_interval",
                                                    self.local_config.get("proactive_interval", 0)) or 0)
                if interval <= 0: continue

                q_start = float(self.get_bot_config(bot_id, "quiet_start",
                                                  self.local_config.get("quiet_start", -1)) or -1)
                q_end = float(self.get_bot_config(bot_id, "quiet_end",
                                                self.local_config.get("quiet_end", -1)) or -1)
                if q_start != -1 and q_end != -1:
                    h = datetime.datetime.now().hour
                    is_quiet = False
                    if q_start > q_end:
                        if h >= q_start or h < q_end: is_quiet = True
                    else:
                        if q_start <= h < q_end: is_quiet = True
                    if is_quiet: continue

                last_active = self.last_active_times.get(bot_id, 0)
                if last_active <= 0: continue  # 该 bot 还没收到过消息，跳过
                if time.time() - last_active <= (interval * 60): continue

                self.last_active_times[bot_id] = time.time()
                provider = self.context.get_using_provider()
                uid = self.last_uids.get(bot_id)
                if not (provider and uid): continue

                try:
                    print(f"👋 [Meme][{bot_id}] 主动发起聊天...", flush=True)

                    # 从DB读该 bot 最近10条对话
                    try:
                        conn = sqlite3.connect(self._db_path())
                        try:
                            c = conn.cursor()
                            c.execute("SELECT content FROM memories WHERE type='dialogue' AND bot_id=? ORDER BY created_at DESC LIMIT 10", (bot_id,))
                            rows = c.fetchall()
                            recent_log = "\n".join([r[0] for r in reversed(rows)])
                        finally:
                            conn.close()
                    except:
                        recent_log = ""

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

                    resp = await provider.text_chat(prompt, session_id=self.last_session_ids.get(bot_id))
                    text = (getattr(resp, "completion_text", None) or getattr(resp, "text", "")).strip()

                    if text:
                        text = self.clean_markdown(text)
                        self._buf(bot_id).append(f"AI (Proactive): {text}")
                        self.save_buffer_to_disk()

                        pattern = r"(<MEME:.*?>|MEME_TAG:\s*[\S]+)"
                        parts = re.split(pattern, text)
                        chain = []

                        for part in parts:
                            tag = None
                            if part.startswith("<MEME:"): tag = part[6:-1].strip()
                            elif "MEME_TAG:" in part: tag = part.replace("MEME_TAG:", "").strip()

                            if tag:
                                path = self.find_best_match(tag, bot_id=bot_id)
                                if path:
                                    print(f"🎯 [Meme][{bot_id}] 主动聊天命中: [{tag}]", flush=True)
                                    chain.append(Image.fromFileSystem(path))
                            elif part:
                                if part.strip():
                                    chain.append(Plain(part))

                        if not chain: continue

                        segments = self.smart_split(chain)

                        delay_base = float(self.get_bot_config(bot_id, "delay_base", self.local_config.get("delay_base", 0.5)))
                        delay_factor = float(self.get_bot_config(bot_id, "delay_factor", self.local_config.get("delay_factor", 0.1)))

                        for i, seg in enumerate(segments):
                            txt_len = sum(len(c.text) for c in seg if isinstance(c, Plain))
                            wait = delay_base + (txt_len * delay_factor)

                            mc = MessageChain()
                            mc.chain = seg
                            await self.context.send_message(uid, mc)
                            if i < len(segments) - 1: await asyncio.sleep(wait)

                except Exception as e:
                    print(f"❌ [Meme][{bot_id}] 主动聊天出错: {e}", flush=True)

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

    def find_best_match(self, query, bot_id=None):
        """从 SQLite 查找最佳匹配的表情包文件路径。v3.0: 全局 ∪ bot 自有"""
        bot_id = bot_id or self.DEFAULT_BOT_ID
        # 1. 尝试直接查库 (全局 + bot 自有)
        conn = sqlite3.connect(self._db_path())
        try:
            c = conn.cursor()
            c.execute("SELECT filename FROM memes WHERE tags LIKE ? AND (bot_id IS NULL OR bot_id=?) LIMIT 1",
                      (f"%{query}%", bot_id))
            row = c.fetchone()
        finally:
            conn.close()

        if row:
            print(f"🔍 [Meme Match] DB精确命中: query='{query}' → {row[0]}", flush=True)
            return os.path.join(self.img_dir, row[0])

        # 2. 如果库里没查到，再遍历 self.data (legacy 兼容，不区分 bot)
        threshold = float(self.get_bot_config(bot_id, "meme_match_threshold",
                                              self.local_config.get("meme_match_threshold", 0.3)))
        best, score = None, 0
        for f, i in self.data.items():
            t = i.get("tags", "")
            if query in t:
                print(f"🔍 [Meme Match] data精确命中: query='{query}' → {f}", flush=True)
                return os.path.join(self.img_dir, f)
            name = t.split(":")[0] if ":" in t else t
            desc = t.split(":")[-1] if ":" in t else ""
            s = max(
                difflib.SequenceMatcher(None, query, name).ratio(),
                difflib.SequenceMatcher(None, query, desc).ratio()
            )
            if s > score: score = s; best = f

        print(f"🔍 [Meme Match] 模糊匹配: query='{query}', 最佳={best}, 分数={score:.2f}, 阈值={threshold}", flush=True)
        if score >= threshold: return os.path.join(self.img_dir, best)
        return None

    
    def save_config(self):
        try:
            with open(self.config_file, "w") as f: json.dump(self.local_config, f, indent=2)
        except: pass
    def load_data(self):
        if not os.path.exists(self.data_file): return {}
        with open(self.data_file, "r") as f: return json.load(f)
    def save_data(self):
        with open(self.data_file, "w") as f: json.dump(self.data, f, ensure_ascii=False)
    def load_buffer_from_disk(self):
        """v3.0: 返回 {bot_id: [...]} 字典；兼容旧版纯 list (迁移到 default bot)"""
        try:
            with open(self.buffer_file, "r") as f:
                raw = json.load(f)
            if isinstance(raw, list):
                # 旧格式：迁移到 default bot
                return {self.DEFAULT_BOT_ID: raw}
            if isinstance(raw, dict):
                # 新格式
                return {k: (v if isinstance(v, list) else []) for k, v in raw.items()}
        except: pass
        return {}
    def save_buffer_to_disk(self):
        try:
            with open(self.buffer_file, "w") as f: json.dump(self.chat_history_buffers, f, ensure_ascii=False)
        except: pass

    def _buf(self, bot_id):
        """获取某个 bot 的 buffer 列表 (不存在则创建)"""
        bot_id = bot_id or self.DEFAULT_BOT_ID
        if bot_id not in self.chat_history_buffers:
            self.chat_history_buffers[bot_id] = []
        return self.chat_history_buffers[bot_id]
    def load_memory(self):
        try:
            with open(self.memory_file, "r", encoding="utf-8") as f: return f.read()
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
        app.router.add_get("/meme_count", self.h_meme_count)
        # === DB Management API ===
        app.router.add_get("/api/db/list", self.h_db_list)
        app.router.add_post("/api/db/delete", self.h_db_delete)
        app.router.add_post("/api/db/edit", self.h_db_edit)
        # === Multi-Bot API (v3.0) ===
        app.router.add_get("/api/bots", self.h_bots_list)
        app.router.add_post("/api/bots/rename", self.h_bots_rename)
        app.router.add_post("/api/bots/delete", self.h_bots_delete)
        app.router.add_get("/api/bot_config", self.h_bot_config_get)
        app.router.add_post("/api/bot_config", self.h_bot_config_set)
        runner = web.AppRunner(app)
        await runner.setup()
        port = self.local_config.get("web_port", 5000)
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        print(f"🌐 [Meme] WebUI 管理后台已启动: http://localhost:{port}", flush=True)


    async def h_up(self, r):
        """上传接口：直接写入 SQLite。
        支持 form 字段 bot_id：留空 / "global" / "" → 写全局 (bot_id=NULL)，否则归属指定 bot。"""
        if not self.check_auth(r): return web.Response(status=403)
        rd = await r.multipart()
        tag = "未分类"
        target_bot_raw = ""  # form 字段
        files_to_insert = []

        while True:
            p = await rd.next()
            if not p: break
            if p.name == "tags": tag = await p.text()
            elif p.name == "bot_id": target_bot_raw = (await p.text() or "").strip()
            elif p.name == "file":
                raw = await p.read()
                comp, ext = await self._compress_image(raw)
                fn = f"{int(time.time()*1000)}_{random.randint(100,999)}{ext}"
                with open(os.path.join(self.img_dir, fn), "wb") as f: f.write(comp)
                h = await self._calc_hash_async(comp)
                files_to_insert.append((fn, tag, h, time.time()))

        # 解析 bot_id：global/空 → NULL；否则字符串
        target_bot = None if target_bot_raw in ("", "global", "all", None) else target_bot_raw

        async with self.db_lock:
            conn = sqlite3.connect(self._db_path())
            try:
                c = conn.cursor()
                for row in files_to_insert:
                    try:
                        c.execute("INSERT INTO memes (filename, tags, source, feature_hash, created_at, bot_id) VALUES (?, ?, 'manual', ?, ?, ?)",
                                  (*row, target_bot))
                    except sqlite3.IntegrityError: pass
                conn.commit()
            finally:
                conn.close()
        return web.Response(text="ok")

    async def h_del(self, r):
        """删除接口：同步删除文件和数据库记录"""
        if not self.check_auth(r): return web.Response(status=403)
        data = await r.json()
        filenames = data.get("filenames", [])
        
        conn = sqlite3.connect(self._db_path())
        try:
            c = conn.cursor()
            for f in filenames:
                try: os.remove(os.path.join(self.img_dir, f))
                except: pass
                c.execute("DELETE FROM memes WHERE filename=?", (f,))
            conn.commit()
            return web.Response(text="ok")
        finally:
            conn.close()

    async def h_tag(self, r):
        """修改标签接口"""
        if not self.check_auth(r): return web.Response(status=403)
        d = await r.json()

        conn = sqlite3.connect(self._db_path())
        try:
            c = conn.cursor()
            c.execute("UPDATE memes SET tags=? WHERE filename=?", (d['tags'], d['filename']))
            conn.commit()
            return web.Response(text="ok")
        finally:
            conn.close()

    async def h_idx(self, r):
        """首页：从数据库读取列表 (含 bot_id 维度)。前端按需筛选/切换。"""
        if not self.check_auth(r): return web.Response(status=403, text="Need ?token=xxx")

        conn = sqlite3.connect(self._db_path())
        try:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("SELECT filename, tags, source, bot_id FROM memes ORDER BY created_at DESC")
            rows = c.fetchall()
        finally:
            conn.close()

        # 结构: {"filename": {"tags": "xxx", "source": "manual", "bot_id": null|str}, ...}
        data_for_web = {row['filename']: {
            "tags": row['tags'],
            "source": row['source'],
            "bot_id": row['bot_id'],
        } for row in rows}

        bots = self.list_bots()  # [{bot_id, nickname, ...}]

        token = self.local_config["web_token"]
        html = (self.read_file("index.html")
                .replace("{{ MEME_DATA }}", json.dumps(data_for_web))
                .replace("{{ BOTS_DATA }}", json.dumps(bots))
                .replace("admin123", token))
        return web.Response(text=html, content_type="text/html")
    # === 配置 key 类型分类 (统一逻辑共用) ===
    _STR_CONFIG_KEYS = {'web_token', 'ai_prompt', 'smtp_host', 'smtp_user',
                        'smtp_pass', 'email_to', 'target_bot_id', 'typing_debounce'}

    async def h_gcf(self, r):
        """返回全局 local_config (兼容旧前端)。
        新前端可加 ?bot_id=xxx 拿该 bot 的有效配置 (effective)。"""
        if not self.check_auth(r): return web.Response(status=403)
        bot_id = (r.query.get("bot_id") or "").strip()
        if not bot_id:
            # 旧行为：返回全局 config
            return web.json_response(self.local_config)
        # 新行为：返回 {global, bot, effective}
        return web.json_response({
            "global": self.local_config,
            "bot": self.get_all_bot_config(bot_id),
            "effective": self.get_effective_bot_config(bot_id),
            "global_keys": sorted(self.GLOBAL_CONFIG_KEYS),
            "bot_keys": sorted(self.BOT_CONFIG_KEYS),
        })

    def _coerce_config_value(self, k, v):
        """按 key 把入参规范化：字符串型 vs 浮点型"""
        if k in self._STR_CONFIG_KEYS:
            val = str(v) if v is not None else ""
            if val.lower() == "none": val = ""
            return val, True
        # 浮点型
        if v is None: return None, False
        s = str(v).strip()
        if s == "" or s.lower() == "none": return None, False
        try:
            return float(s), True
        except Exception:
            return None, False

    async def h_ucf(self, r):
        """更新配置。
        - 不带 ?bot_id=xxx：写入全局 local_config (兼容旧前端)。
        - 带 ?bot_id=xxx：bot 级 key 写到该 bot 的 bot_configs；全局 key 仍写到 local_config。"""
        if not self.check_auth(r): return web.Response(status=403)
        try:
            new_conf = await r.json()
            target_bot = (r.query.get("bot_id") or "").strip()
            wrote_global = False

            for k, v in new_conf.items():
                norm, ok = self._coerce_config_value(k, v)
                if not ok: continue

                if target_bot and k in self.BOT_CONFIG_KEYS:
                    # 写入 bot_configs 表
                    self.set_bot_config(target_bot, k, norm)
                elif k in self.GLOBAL_CONFIG_KEYS or not target_bot:
                    # 全局 key、或没指定 bot 时：写到 local_config
                    self.local_config[k] = norm
                    wrote_global = True
                else:
                    # 已指定 bot 但 key 不在 BOT_CONFIG_KEYS / GLOBAL_CONFIG_KEYS：兜底为 bot 级
                    self.set_bot_config(target_bot, k, norm)

            if wrote_global:
                self.save_config()
            return web.Response(text="ok")
        except Exception as e:
            return web.Response(status=500, text=str(e))

    async def h_backup(self, r):
        """备份接口。
        - 不带 ?bot_id 或 ?bot_id=all：全量备份 (老行为)，含完整 db / 配置 / 全部图片。
        - ?bot_id=xxx：导出该 bot 的子集 (该 bot 自有 memes 文件 + 全局 NULL memes + 该 bot 的 memories + bot_configs)，
          打包成一个轻量级 zip (含一个 subset.db 和 images/ 子目录)。"""
        if not self.check_auth(r): return web.Response(status=403)
        bot_id = (r.query.get("bot_id") or "").strip()
        b = io.BytesIO()

        if not bot_id or bot_id == "all":
            # 全量备份 (兼容老行为)
            with zipfile.ZipFile(b, 'w', zipfile.ZIP_DEFLATED) as z:
                for root, _, files in os.walk(self.img_dir):
                    for f in files: z.write(os.path.join(root, f), f"images/{f}")
                if os.path.exists(self.data_file): z.write(self.data_file, "memes.json")
                if os.path.exists(self.config_file): z.write(self.config_file, "config.json")
                if os.path.exists(self.buffer_file): z.write(self.buffer_file, "buffer.json")
                db_p = os.path.join(self.base_dir, "meme_core.db")
                if os.path.exists(db_p): z.write(db_p, "meme_core.db")
            fname = "meme_backup_all.zip"
        else:
            # 单 bot 子集备份
            fname = f"meme_backup_{bot_id}.zip"
            subset_db_buf = io.BytesIO()
            # 用内存 sqlite 装子集
            mem_conn = sqlite3.connect(":memory:")
            try:
                src = sqlite3.connect(self._db_path())
                try:
                    src.row_factory = sqlite3.Row
                    sc = src.cursor()
                    mc = mem_conn.cursor()
                    # 复制表结构 (简化版：仅 memes / memories / bot_configs / bots)
                    mc.executescript("""
                        CREATE TABLE memes (filename TEXT PRIMARY KEY, tags TEXT, feature_hash TEXT,
                            source TEXT, created_at REAL, last_used REAL, usage_count INTEGER, bot_id TEXT);
                        CREATE TABLE memories (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT,
                            keywords TEXT, type TEXT, importance INTEGER, created_at REAL, bot_id TEXT);
                        CREATE TABLE bot_configs (bot_id TEXT, key TEXT, value TEXT, PRIMARY KEY (bot_id, key));
                        CREATE TABLE bots (bot_id TEXT PRIMARY KEY, nickname TEXT, created_at REAL, last_active REAL);
                    """)
                    # 该 bot + 全局 NULL 的 memes
                    sc.execute("SELECT * FROM memes WHERE bot_id IS NULL OR bot_id=?", (bot_id,))
                    meme_rows = [dict(x) for x in sc.fetchall()]
                    for row in meme_rows:
                        mc.execute("INSERT INTO memes (filename, tags, feature_hash, source, created_at, last_used, usage_count, bot_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                   (row.get('filename'), row.get('tags'), row.get('feature_hash'),
                                    row.get('source'), row.get('created_at'), row.get('last_used') or 0,
                                    row.get('usage_count') or 0, row.get('bot_id')))
                    # 该 bot 的 memories
                    sc.execute("SELECT * FROM memories WHERE bot_id=?", (bot_id,))
                    for row in [dict(x) for x in sc.fetchall()]:
                        mc.execute("INSERT INTO memories (content, keywords, type, importance, created_at, bot_id) VALUES (?, ?, ?, ?, ?, ?)",
                                   (row.get('content'), row.get('keywords'), row.get('type'),
                                    row.get('importance') or 1, row.get('created_at'), row.get('bot_id')))
                    # 该 bot 的配置 + bots 行
                    sc.execute("SELECT bot_id, key, value FROM bot_configs WHERE bot_id=?", (bot_id,))
                    for r2 in sc.fetchall(): mc.execute("INSERT INTO bot_configs VALUES (?, ?, ?)", tuple(r2))
                    sc.execute("SELECT bot_id, nickname, created_at, last_active FROM bots WHERE bot_id=?", (bot_id,))
                    for r2 in sc.fetchall(): mc.execute("INSERT INTO bots VALUES (?, ?, ?, ?)", tuple(r2))
                    mem_conn.commit()
                finally:
                    src.close()
                # dump 内存 db 到字节
                subset_db_path = os.path.join(self.base_dir, f"_subset_{bot_id}_{int(time.time())}.db")
                file_conn = sqlite3.connect(subset_db_path)
                try:
                    mem_conn.backup(file_conn)
                finally:
                    file_conn.close()
                with open(subset_db_path, "rb") as f: subset_db_bytes = f.read()
                try: os.remove(subset_db_path)
                except: pass
            finally:
                mem_conn.close()

            # 打包：subset.db + 该 bot 涉及的图片 (含全局)
            referenced = {row['filename'] for row in meme_rows}
            with zipfile.ZipFile(b, 'w', zipfile.ZIP_DEFLATED) as z:
                z.writestr("meme_core.db", subset_db_bytes)
                # 备份元信息
                z.writestr("backup_info.json", json.dumps({
                    "bot_id": bot_id, "kind": "single_bot",
                    "exported_at": time.time(), "meme_count": len(referenced),
                }, ensure_ascii=False, indent=2))
                for f in referenced:
                    p = os.path.join(self.img_dir, f)
                    if os.path.exists(p): z.write(p, f"images/{f}")

        b.seek(0)
        return web.Response(body=b, headers={'Content-Disposition': f'attachment; filename="{fname}"'})

    # === 多 Bot 管理 API ===
    async def h_bots_list(self, r):
        """列出所有已注册的 bot (含自动注册的)。"""
        if not self.check_auth(r): return web.Response(status=403)
        bots = self.list_bots()
        # 顺带统计每个 bot 的 meme/memory 数
        try:
            conn = sqlite3.connect(self._db_path())
            try:
                c = conn.cursor()
                for b in bots:
                    bid = b["bot_id"]
                    c.execute("SELECT COUNT(*) FROM memes WHERE bot_id=?", (bid,))
                    b["meme_count"] = c.fetchone()[0]
                    c.execute("SELECT COUNT(*) FROM memories WHERE bot_id=?", (bid,))
                    b["memory_count"] = c.fetchone()[0]
                # 全局共享池数量 (展示用)
                c.execute("SELECT COUNT(*) FROM memes WHERE bot_id IS NULL")
                global_meme = c.fetchone()[0]
            finally:
                conn.close()
        except Exception:
            global_meme = 0
        return web.json_response({"bots": bots, "global_meme_count": global_meme})

    async def h_bots_rename(self, r):
        """重命名 bot 昵称 (不改 bot_id)。"""
        if not self.check_auth(r): return web.Response(status=403)
        d = await r.json()
        bid = (d.get("bot_id") or "").strip()
        nick = (d.get("nickname") or "").strip()
        if not bid or not nick: return web.json_response({"ok": False, "msg": "missing bot_id or nickname"})
        try:
            conn = sqlite3.connect(self._db_path(), timeout=5)
            try:
                c = conn.cursor()
                c.execute("UPDATE bots SET nickname=? WHERE bot_id=?", (nick, bid))
                conn.commit()
            finally:
                conn.close()
            return web.json_response({"ok": True})
        except Exception as e:
            return web.json_response({"ok": False, "msg": str(e)})

    async def h_bots_delete(self, r):
        """彻底删除一个 bot 及其归属的 memories / memes / 配置 (危险操作)。
        全局 NULL memes 不会被删。"""
        if not self.check_auth(r): return web.Response(status=403)
        d = await r.json()
        bid = (d.get("bot_id") or "").strip()
        if not bid: return web.json_response({"ok": False, "msg": "missing bot_id"})
        if bid == self.DEFAULT_BOT_ID:
            return web.json_response({"ok": False, "msg": "默认 bot 不可删除"})
        try:
            async with self.db_lock:
                conn = sqlite3.connect(self._db_path(), timeout=10)
                try:
                    c = conn.cursor()
                    # 先把该 bot 的 memes 文件物理删除
                    c.execute("SELECT filename FROM memes WHERE bot_id=?", (bid,))
                    for (fn,) in c.fetchall():
                        try: os.remove(os.path.join(self.img_dir, fn))
                        except: pass
                    c.execute("DELETE FROM memes WHERE bot_id=?", (bid,))
                    c.execute("DELETE FROM memories WHERE bot_id=?", (bid,))
                    c.execute("DELETE FROM bot_configs WHERE bot_id=?", (bid,))
                    c.execute("DELETE FROM bots WHERE bot_id=?", (bid,))
                    conn.commit()
                finally:
                    conn.close()
            # 内存态也清理
            self.chat_history_buffers.pop(bid, None)
            self.last_active_times.pop(bid, None)
            self.last_uids.pop(bid, None)
            self.last_session_ids.pop(bid, None)
            self.last_auto_save_times.pop(bid, None)
            self.summarizing_bots.discard(bid)
            self.save_buffer_to_disk()
            return web.json_response({"ok": True})
        except Exception as e:
            return web.json_response({"ok": False, "msg": str(e)})

    async def h_bot_config_get(self, r):
        """获取某 bot 的有效配置 (bot 覆盖 + 全局兜底)。"""
        if not self.check_auth(r): return web.Response(status=403)
        bid = (r.query.get("bot_id") or self.DEFAULT_BOT_ID).strip() or self.DEFAULT_BOT_ID
        return web.json_response({
            "bot_id": bid,
            "overrides": self.get_all_bot_config(bid),
            "effective": self.get_effective_bot_config(bid),
            "bot_keys": sorted(self.BOT_CONFIG_KEYS),
        })

    async def h_bot_config_set(self, r):
        """批量设置某 bot 的配置 (仅 BOT_CONFIG_KEYS)。
        Body: {"bot_id": "xxx", "config": {...}}"""
        if not self.check_auth(r): return web.Response(status=403)
        try:
            d = await r.json()
            bid = (d.get("bot_id") or "").strip() or self.DEFAULT_BOT_ID
            cfg = d.get("config") or {}
            if not isinstance(cfg, dict):
                return web.json_response({"ok": False, "msg": "config must be object"})
            written = 0
            for k, v in cfg.items():
                if k not in self.BOT_CONFIG_KEYS: continue
                norm, ok = self._coerce_config_value(k, v)
                if ok:
                    self.set_bot_config(bid, k, norm)
                    written += 1
            return web.json_response({"ok": True, "written": written})
        except Exception as e:
            return web.json_response({"ok": False, "msg": str(e)})

    async def h_restore(self, r):
        if not self.check_auth(r): return web.Response(status=403)
        try:
            reader = await r.multipart()
            field = await reader.next()
            file_data = await field.read()
            
            response = web.StreamResponse()
            response.content_type = 'text/plain'
            await response.prepare(r)

            def unzip_action():
                with zipfile.ZipFile(io.BytesIO(file_data), 'r') as z: 
                    z.extractall(self.base_dir)
            
            loop = asyncio.get_running_loop()
            task = loop.create_task(loop.run_in_executor(self.executor, unzip_action))
            
            # 发送心跳包保持连接，防止 Cloudflare 等反代的 524 超时
            while not task.done():
                try:
                    await response.write(b" ")
                    await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
                except asyncio.TimeoutError:
                    pass
            
            await task
            
            # 重新加载数据
            self.data = self.load_data()
            self.local_config = self.load_config()
            self.chat_history_buffers = self.load_buffer_from_disk()
            self.config_mtime = os.path.getmtime(self.config_file) if os.path.exists(self.config_file) else 0
            self.init_db()
            
            await response.write(b"ok")
            await response.write_eof()
            return response
        except Exception as e:
            return web.Response(status=500, text=str(e))

    async def h_import_legacy(self, r):
        """WebUI 接口：接收旧数据文件并导入"""
        if not self.check_auth(r): return web.Response(status=403, text="Forbidden")
        try:
            reader = await r.multipart()
            memes_data, memory_text, buffer_data = None, "", []

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

            success, msg = self.merge_legacy_data(memes_data, memory_text, buffer_data)
            return web.Response(text=msg if success else "Error: " + msg)
            
        except Exception as e:
            return web.Response(status=500, text=f"Server Error: {str(e)}")

    async def h_get_stickies(self, r):
        """获取 sticky 列表。支持 ?bot_id=xxx 或 ?bot_id=all (默认 all 含所有 bot)。"""
        if not self.check_auth(r): return web.Response(status=403)
        bid = (r.query.get("bot_id") or "all").strip()
        conn = sqlite3.connect(self._db_path())
        try:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            if bid and bid != "all":
                c.execute("SELECT id, content, created_at, bot_id FROM memories WHERE type='sticky' AND bot_id=? ORDER BY created_at DESC", (bid,))
            else:
                c.execute("SELECT id, content, created_at, bot_id FROM memories WHERE type='sticky' ORDER BY created_at DESC")
            rows = [dict(ix) for ix in c.fetchall()]
            return web.json_response(rows)
        finally:
            conn.close()

    async def h_update_sticky(self, r):
        if not self.check_auth(r): return web.Response(status=403)
        data = await r.json()
        action = data.get('action') # add / delete / edit
        # 多 bot: 允许指定 bot_id (前端尚未支持时回退到 default)
        target_bot = (data.get('bot_id') or self.DEFAULT_BOT_ID).strip() or self.DEFAULT_BOT_ID

        async with self.db_lock:
            conn = sqlite3.connect(self._db_path())
            try:
                c = conn.cursor()

                if action == 'add':
                    content = data.get('content', '').strip()
                    if content:
                        c.execute("INSERT INTO memories (content, type, importance, created_at, bot_id) VALUES (?, 'sticky', 10, ?, ?)",
                                 (content, time.time(), target_bot))

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
                # 重置该 bot 的 sticky 注入计数器，触发下次必读
                c.execute("INSERT OR REPLACE INTO system_config (key, value) VALUES (?, ?)", (f"round_count_{target_bot}", "0"))
                conn.commit()
                print(f"🔍 [Sticky] WebUI更新 bot={target_bot}，已重置 round_count", flush=True)
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

    async def h_meme_count(self, r):
        """统计图片数。?bot_id=xxx 表示该 bot 视角 (NULL∪bot)；?bot_id=all 全部；不传 = 全部。"""
        if not self.check_auth(r): return web.Response(status=403)
        bid = (r.query.get("bot_id") or "all").strip()
        conn = sqlite3.connect(self._db_path())
        try:
            c = conn.cursor()
            if bid and bid != "all":
                c.execute("SELECT COUNT(*) FROM memes WHERE bot_id IS NULL OR bot_id=?", (bid,))
            else:
                c.execute("SELECT COUNT(*) FROM memes")
            count = c.fetchone()[0]
            return web.json_response({"count": count, "bot_id": bid})
        finally:
            conn.close()

    # === DB Management Endpoints ===
    async def h_db_list(self, r):
        """列出 memories 表记录 (分页 + 类型筛选 + 搜索 + bot 筛选)"""
        if not self.check_auth(r): return web.Response(status=403)
        mem_type = r.query.get("type", "dialogue")  # dialogue/fragment/sticky
        page = int(r.query.get("page", 1))
        search = r.query.get("search", "").strip()
        bid = (r.query.get("bot_id") or "all").strip()
        page_size = 30
        offset = (page - 1) * page_size

        # 动态拼 WHERE
        where = ["type=?"]
        params = [mem_type]
        if bid and bid != "all":
            where.append("bot_id=?")
            params.append(bid)
        if search:
            where.append("content LIKE ?")
            params.append(f"%{search}%")
        where_sql = " AND ".join(where)

        conn = sqlite3.connect(self._db_path())
        try:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute(f"SELECT COUNT(*) FROM memories WHERE {where_sql}", params)
            total = c.fetchone()[0]
            c.execute(f"SELECT id, content, keywords, created_at, bot_id FROM memories WHERE {where_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                      (*params, page_size, offset))
            rows = [{"id": r["id"], "content": r["content"], "keywords": r["keywords"] or "",
                     "created_at": r["created_at"], "bot_id": r["bot_id"]} for r in c.fetchall()]
            return web.json_response({"total": total, "page": page, "page_size": page_size, "rows": rows})
        finally:
            conn.close()

    async def h_db_delete(self, r):
        """删除指定 memories 记录"""
        if not self.check_auth(r): return web.Response(status=403)
        data = await r.json()
        ids = data.get("ids", [])
        if not ids: return web.json_response({"ok": False, "msg": "no ids"})
        
        conn = sqlite3.connect(self._db_path())
        try:
            c = conn.cursor()
            placeholders = ",".join(["?"] * len(ids))
            c.execute(f"DELETE FROM memories WHERE id IN ({placeholders})", ids)
            conn.commit()
            deleted = c.rowcount
            return web.json_response({"ok": True, "deleted": deleted})
        finally:
            conn.close()

    async def h_db_edit(self, r):
        """编辑指定 memories 记录的 content"""
        if not self.check_auth(r): return web.Response(status=403)
        data = await r.json()
        mid = data.get("id")
        content = data.get("content", "").strip()
        if not mid or not content: return web.json_response({"ok": False, "msg": "missing id or content"})
        
        # 重新提取关键词
        kw = " ".join(jieba.analyse.extract_tags(content, topK=8))
        
        conn = sqlite3.connect(self._db_path())
        try:
            c = conn.cursor()
            c.execute("UPDATE memories SET content=?, keywords=? WHERE id=?", (content, kw, mid))
            conn.commit()
            updated = c.rowcount
            return web.json_response({"ok": True, "updated": updated})
        finally:
            conn.close()

    async def send_backup_email(self):
        conf = self.local_config
        host = conf.get("smtp_host")
        user = conf.get("smtp_user")
        pw = conf.get("smtp_pass")
        to_email = conf.get("email_to")
        
        if not all([host, user, pw, to_email]): return "配置不全：请检查SMTP主机、账号、授权码和收件人"

        try:
            # 1. 计算大小
            img_size = 0
            for root, _, files in os.walk(self.img_dir):
                img_size += sum(os.path.getsize(os.path.join(root, name)) for name in files)
            
            include_images = img_size < (20 * 1024 * 1024)
            msg_body = "MemeMaster 自动备份。\n"
            
            # 2. 制作 ZIP
            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as z:
                db_path = os.path.join(self.base_dir, "meme_core.db")
                if os.path.exists(db_path): z.write(db_path, "meme_core.db")
                if os.path.exists(self.config_file): z.write(self.config_file, "config.json")
                
                if include_images:
                    for root, _, files in os.walk(self.img_dir):
                        for f in files: z.write(os.path.join(root, f), f"images/{f}")
                    msg_body += "✅ 包含完整图片库。"
                else:
                    z.writestr("README.txt", "图片库过大，未包含。")
                    msg_body += f"⚠️ 图片库过大 ({img_size/1024/1024:.1f}MB)，仅备份了数据库和配置。"
            
            zip_buffer.seek(0)

            # 3. 组装邮件 (这里就是之前缺少的步骤)
            msg = MIMEMultipart()
            msg['Subject'] = f"MemeMaster 备份 - {datetime.datetime.now().strftime('%Y-%m-%d')}"
            msg['From'] = user
            msg['To'] = to_email
            
            # 添加正文
            msg.attach(MIMEText(msg_body, 'plain', 'utf-8'))
            
            # 添加附件
            part = MIMEBase('application', "octet-stream")
            part.set_payload(zip_buffer.read())
            encoders.encode_base64(part)
            part.add_header('Content-Disposition', 'attachment; filename="meme_backup.zip"')
            msg.attach(part)

            # 4. 发送
            def _send():
                with smtplib.SMTP_SSL(host, 465) as server:
                    server.login(user, pw)
                    server.send_message(msg)
            
            await asyncio.get_running_loop().run_in_executor(self.executor, _send)
            return "✅ 备份邮件已发送，请查收"

        except Exception as e:
            return f"❌ 发送失败: {str(e)}"
