from motor.motor_asyncio import AsyncIOMotorClient
from config.config import Config
import os
import logging
import asyncio
import re
import unicodedata
from bson import ObjectId

logger = logging.getLogger("ANIZONEFLIX_DB")

def clean_doc(doc):
    """Recursively convert ObjectId to string for JSON serialization"""
    if doc is None:
        return None
    if isinstance(doc, list):
        return [clean_doc(d) for d in doc]
    if isinstance(doc, dict):
        new_doc = {}
        for k, v in doc.items():
            if isinstance(v, ObjectId):
                new_doc[k] = str(v)
            elif isinstance(v, (dict, list)):
                new_doc[k] = clean_doc(v)
            else:
                new_doc[k] = v
        return new_doc
    return doc

class Database:
    def __init__(self):
        self.client = None
        self._db = None
        self._anime = None
        self._episodes = None
        self._users = None
        self._categories = None
        self._schedules = None
        self._songs = None
        self._settings = None

    async def connect(self):
        """Initialize connection with absolute persistence focus and retries"""
        if self.client:
            return

        uri = Config.MONGO_URI
        if not uri:
            logger.error("MONGO_URI is missing; refusing to connect to an implicit localhost database.")
            raise RuntimeError("MONGO_URI is required. Configure MongoDB Atlas or another persistent MongoDB instance.")

        for attempt in range(1, 4):
            try:
                logger.info(f"Connecting to MongoDB Atlas (Attempt {attempt}/5)...")
                self.client = AsyncIOMotorClient(
                    uri,
                    serverSelectionTimeoutMS=7000,
                    connectTimeoutMS=10000,
                    socketTimeoutMS=15000,
                    maxPoolSize=20,
                    minPoolSize=0,
                    retryWrites=True,
                    retryReads=True,
                    appname="AniZoneFlix-Executive"
                )
                await self.client.admin.command('ping')

                self._db = self.client[Config.DB_NAME]
                self._anime = self._db.anime
                self._episodes = self._db.episodes
                self._users = self._db.users
                self._categories = self._db.categories
                self._schedules = self._db.schedules
                self._songs = self._db.songs
                self._settings = self._db.settings

                # Keep common catalogue lookups bounded and predictable.
                index_jobs = [
                    (self._anime, [("updated_at", -1), ("_id", -1)], {"name": "catalogue_recent_idx"}),
                    (self._anime, "slug", {"name": "catalogue_slug_idx", "sparse": True}),
                    (self._anime, "category", {"name": "catalogue_category_idx"}),
                    (self._categories, "name", {"name": "category_name_idx", "sparse": True}),
                    (self._settings, "key", {"name": "settings_key_idx", "sparse": True}),
                ]
                for collection, keys, options in index_jobs:
                    try:
                        await collection.create_index(keys, **options)
                    except Exception as index_error:
                        logger.warning("Optional index %s skipped: %s", options.get("name"), index_error)

                logger.info(f"Database Persistence Verified: {Config.DB_NAME} is active.")
                return
            except Exception as e:
                logger.error(f"Database connection blocked on attempt {attempt}: {e}")
                if attempt == 3:
                    logger.critical("FINAL PERSISTENCE FAILURE: System cannot guarantee data safety.")
                    raise e
                await asyncio.sleep(attempt * 1.5)

    @property
    def anime(self):
        return self._anime if self._anime is not None else self.MockCollection("anime")

    @property
    def episodes(self):
        return self._episodes if self._episodes is not None else self.MockCollection("episodes")

    @property
    def users(self):
        return self._users if self._users is not None else self.MockCollection("users")

    @property
    def categories(self):
        return self._categories if self._categories is not None else self.MockCollection("categories")

    @property
    def schedules(self):
        return self._schedules if self._schedules is not None else self.MockCollection("schedules")

    @property
    def songs(self):
        return self._songs if self._songs is not None else self.MockCollection("songs")

    @property
    def settings(self):
        return self._settings if self._settings is not None else self.MockCollection("settings")

    @property
    def added_bots(self):
        return self._db.added_bots if self._db is not None else self.MockCollection("added_bots")

    # --- Uptime Bot Monitoring ---

    async def add_monitored_bot(self, url, name=None):
        try:
            coll = self.added_bots
            bot_id = str(ObjectId())
            if not name:
                name = url.replace("https://", "").replace("http://", "").split("/")[0]
            data = {
                "bot_id": bot_id,
                "url": url,
                "name": name,
                "status": "pending",
                "status_code": None,
                "response_time_ms": None,
                "last_checked": None,
                "created_at": asyncio.get_event_loop().time()
            }
            await coll.update_one({"bot_id": bot_id}, {"$set": data}, upsert=True)
            return bot_id
        except Exception as e:
            logger.error(f"Persistence Error (add_monitored_bot): {e}")
            return None

    async def get_all_monitored_bots(self):
        try:
            coll = self.added_bots
            cursor = coll.find()
            docs = await cursor.to_list(length=1000)
            return clean_doc(docs) or []
        except Exception as e:
            logger.error(f"Read Error (get_all_monitored_bots): {e}")
            return []

    async def get_monitored_bot(self, bot_id):
        try:
            coll = self.added_bots
            doc = await coll.find_one({"bot_id": bot_id})
            if not doc and len(bot_id) == 24:
                try:
                    doc = await coll.find_one({"_id": ObjectId(bot_id)})
                except: pass
            return clean_doc(doc)
        except Exception as e:
            logger.error(f"Read Error (get_monitored_bot): {e}")
            return None

    async def delete_monitored_bot(self, bot_id):
        try:
            coll = self.added_bots
            res = await coll.delete_one({"bot_id": bot_id})
            if res and res.deleted_count == 0 and len(bot_id) == 24:
                try:
                    res = await coll.delete_one({"_id": ObjectId(bot_id)})
                except: pass
            return res
        except Exception as e:
            logger.error(f"Persistence Error (delete_monitored_bot): {e}")
            return None

    async def replace_monitored_bot(self, bot_id, new_url, name=None):
        try:
            coll = self.added_bots
            if not name:
                name = new_url.replace("https://", "").replace("http://", "").split("/")[0]
            update_data = {
                "url": new_url,
                "name": name,
                "status": "pending",
                "status_code": None,
                "response_time_ms": None
            }
            res = await coll.update_one({"bot_id": bot_id}, {"$set": update_data})
            if res and res.matched_count == 0 and len(bot_id) == 24:
                try:
                    res = await coll.update_one({"_id": ObjectId(bot_id)}, {"$set": update_data})
                except: pass
            return res
        except Exception as e:
            logger.error(f"Persistence Error (replace_monitored_bot): {e}")
            return None

    async def update_monitored_bot_status(self, bot_id, status, status_code=None, response_time_ms=None):
        try:
            coll = self.added_bots
            import time
            update_data = {
                "status": status,
                "status_code": status_code,
                "response_time_ms": response_time_ms,
                "last_checked": time.time()
            }
            res = await coll.update_one({"bot_id": bot_id}, {"$set": update_data})
            if res and res.matched_count == 0 and len(bot_id) == 24:
                try:
                    res = await coll.update_one({"_id": ObjectId(bot_id)}, {"$set": update_data})
                except: pass
            return res
        except Exception as e:
            logger.error(f"Persistence Error (update_monitored_bot_status): {e}")
            return None

    class MockCollection:
        """Emergency layer to prevent system crashes if Atlas is unreachable"""
        def __init__(self, name):
            self.name = name

        def __getattr__(self, name):
            async def mock_func(*args, **kwargs):
                logger.error(f"SYSTEM DEGRADED: Call to {self.name}.{name} ignored (DB Offline).")
                return None
            return mock_func

        async def find_one(self, *args, **kwargs): return None
        def find(self, *args, **kwargs):
            class MockCursor:
                async def to_list(self, *args, **kwargs): return []
                def sort(self, *args, **kwargs): return self
                def skip(self, *args, **kwargs): return self
                def limit(self, *args, **kwargs): return self
            return MockCursor()
        async def update_one(self, *args, **kwargs): return None
        async def delete_one(self, *args, **kwargs): return None
        async def delete_many(self, *args, **kwargs): return None
        def __bool__(self): return False

    async def ping(self):
        try:
            if self.client is None:
                await self.connect()
            await self.client.admin.command("ping")
            return True
        except Exception as exc:
            logger.warning("MongoDB ping failed; clearing stale connection: %s", exc)
            client = self.client
            self.client = None
            self._db = self._anime = self._episodes = self._users = None
            self._categories = self._schedules = self._songs = self._settings = None
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
            return False

    # --- Robust CRUD Methods (Persistence Focused) ---

    async def add_anime(self, data):
        try:
            if self._anime is None: return None
            return await self._anime.update_one(
                {"mal_id": data["mal_id"]},
                {"$set": data, "$currentDate": {"updated_at": True}},
                upsert=True
            )
        except Exception as e:
            logger.error(f"Persistence Error (add_anime): {e}")
            return None

    async def get_anime_by_slug(self, slug):
        try:
            if self._anime is None: return None
            doc = await self._anime.find_one({"slug": slug})
            return clean_doc(doc)
        except Exception as e:
            logger.error(f"Read Error (get_anime_by_slug): {e}")
            return None

    async def get_anime_by_mal_id(self, mal_id):
        try:
            if self._anime is None: return None
            doc = await self._anime.find_one({"mal_id": mal_id})
            return clean_doc(doc)
        except Exception as e:
            logger.error(f"Read Error (get_anime_by_mal_id): {e}")
            return None

    async def get_all_anime(self, limit=20, skip=0):
        try:
            if self._anime is None and not await self.ping(): return []
            limit = max(1, min(int(limit), 100))
            skip = max(0, int(skip))
            cursor = self._anime.find().sort([("updated_at", -1), ("_id", -1)]).skip(skip).limit(limit)
            docs = await cursor.to_list(length=limit)
            return clean_doc(docs) or []
        except Exception as e:
            logger.error(f"Read Error (get_all_anime): {e}")
            return []

    async def get_anime_by_category(self, category_name: str, limit: int = 48, skip: int = 0):
        """Return anime pages belonging to a category (case-insensitive)."""
        try:
            if self._anime is None and not await self.ping():
                return []
            name = (category_name or "").strip()
            if not name:
                return []
            limit = max(1, min(int(limit), 100))
            skip = max(0, int(skip))
            # Case-insensitive exact match on category field
            pattern = f"^{re.escape(name)}$"
            cursor = self._anime.find(
                {"category": {"$regex": pattern, "$options": "i"}},
                {
                    "_id": 1, "title": 1, "title_english": 1, "slug": 1,
                    "image": 1, "score": 1, "year": 1, "category": 1,
                    "status": 1, "mal_id": 1, "synopsis": 1, "updated_at": 1
                }
            ).sort([("updated_at", -1), ("_id", -1)]).skip(skip).limit(limit)
            return clean_doc(await cursor.to_list(length=limit)) or []
        except Exception as e:
            logger.error("Read Error (get_anime_by_category %s): %s", category_name, e)
            return []

    async def count_anime_by_category(self, category_name: str) -> int:
        """Count anime in a category for pagination."""
        try:
            if self._anime is None and not await self.ping():
                return 0
            name = (category_name or "").strip()
            if not name:
                return 0
            pattern = f"^{re.escape(name)}$"
            return await self._anime.count_documents({"category": {"$regex": pattern, "$options": "i"}})
        except Exception as e:
            logger.error("Read Error (count_anime_by_category %s): %s", category_name, e)
            return 0

    async def search_anime_db(self, query):
        try:
            if self._anime is None: return []
            safe_query = re.escape(query)
            cursor = self._anime.find({"title": {"$regex": safe_query, "$options": "i"}})
            docs = await cursor.to_list(length=10000)
            return clean_doc(docs) or []
        except Exception as e:
            logger.error(f"Read Error (search_anime_db): {e}")
            return []

    async def search_anime_page(self, raw_query: str, skip: int = 0, limit: int = 24):
        """Unicode-safe, bounded catalogue search with database pagination."""
        try:
            if self._anime is None and not await self.ping():
                return []
            query = unicodedata.normalize("NFC", (raw_query or "").strip())[:120]
            if not query:
                return []
            fields = ["title", "title_english", "title_native", "native_title", "synonyms", "alternate_titles"]
            pattern = re.escape(query)
            clauses = [{field: {"$regex": pattern, "$options": "i"}} for field in fields]
            cursor = self._anime.find({"$or": clauses}, {
                "_id": 1, "title": 1, "title_english": 1, "title_native": 1,
                "native_title": 1, "synonyms": 1, "alternate_titles": 1,
                "slug": 1, "image": 1, "score": 1, "year": 1,
                "category": 1, "status": 1, "mal_id": 1
            }).sort("title", 1).skip(max(0, int(skip))).limit(max(1, min(int(limit), 48)))
            return clean_doc(await cursor.to_list(length=max(1, min(int(limit), 48)))) or []
        except Exception as e:
            logger.error("Read Error (search_anime_page): %s", e)
            return []

    async def search_anime_intelligent(self, raw_query: str, limit: int = 50):
        """Search title variants with Unicode-preserving normalization and ranking."""
        try:
            if self._anime is None and not await self.ping():
                return []
            q = unicodedata.normalize("NFC", (raw_query or "").strip())[:120]
            if not q:
                return []
            fields = ["title", "title_english", "title_native", "native_title", "synonyms", "alternate_titles"]
            words = [w for w in re.split(r"[^\w]+", q.casefold(), flags=re.UNICODE) if w]
            conditions = [{field: {"$regex": re.escape(q), "$options": "i"}} for field in fields]
            if words:
                # Match all query terms against at least one searchable title field.
                for field in fields:
                    conditions.append({"$and": [{field: {"$regex": re.escape(w), "$options": "i"}} for w in words]})
            cursor = self._anime.find({"$or": conditions}, {
                "_id": 1, "title": 1, "title_english": 1, "title_native": 1,
                "native_title": 1, "synonyms": 1, "alternate_titles": 1,
                "slug": 1, "image": 1, "score": 1, "year": 1,
                "category": 1, "status": 1, "mal_id": 1
            }).limit(max(100, min(int(limit) * 8, 800)))
            docs = clean_doc(await cursor.to_list(length=max(100, min(int(limit) * 8, 800)))) or []

            def values(doc):
                out = []
                for field in fields:
                    value = doc.get(field)
                    if isinstance(value, str): out.append(value)
                    elif isinstance(value, (list, tuple)):
                        out.extend(str(v) for v in value if v is not None)
                return out

            q_norm = re.sub(r"[^\w]", "", q.casefold(), flags=re.UNICODE)
            def score(doc):
                best = 0
                for value in values(doc):
                    v = value.casefold()
                    norm = re.sub(r"[^\w]", "", v, flags=re.UNICODE)
                    if norm == q_norm: best = max(best, 3000)
                    elif norm.startswith(q_norm): best = max(best, 2200)
                    elif q_norm and q_norm in norm: best = max(best, 1600)
                    elif words:
                        matched = sum(1 for word in words if word in v)
                        best = max(best, int(1000 * matched / len(words)))
                return best

            ranked = [(score(doc), doc) for doc in docs]
            ranked.sort(key=lambda pair: (-pair[0], str(pair[1].get("title", "")).casefold()))
            return [doc for points, doc in ranked if points > 0][:max(1, int(limit))]
        except Exception as e:
            logger.error("Read Error (search_anime_intelligent): %s", e)
            return []

    async def get_anime(self, identifier):
        """Robust lookup by ID string or Slug"""
        if not identifier: return None
        try:
            if self._anime is None: return None

            # Try ObjectId lookup first if it looks like one
            if isinstance(identifier, str) and len(identifier) == 24:
                try:
                    doc = await self._anime.find_one({"_id": ObjectId(identifier)})
                    if doc: return clean_doc(doc)
                except: pass

            # Fallback to slug
            doc = await self._anime.find_one({"slug": identifier})
            return clean_doc(doc)
        except Exception as e:
            logger.error(f"Read Error (get_anime): {e}")
            return None

    async def delete_anime_by_slug(self, slug):
        try:
            if self._anime is None: return None
            anime = await self.get_anime_by_slug(slug)
            if anime:
                if self._episodes is not None:
                    await self._episodes.delete_many({"mal_id": anime["mal_id"]})
            return await self._anime.delete_one({"slug": slug})
        except Exception as e:
            logger.error(f"Sanitization Error (delete_anime_by_slug): {e}")
            return None

    async def add_episode(self, data):
        try:
            if self._episodes is None: return None
            query = {"mal_id": data["mal_id"], "season": data.get("season"), "episode": data.get("episode"), "quality": data.get("quality")}
            return await self._episodes.update_one(query, {"$set": data}, upsert=True)
        except Exception as e:
            logger.error(f"Persistence Error (add_episode): {e}")
            return None

    async def get_episodes(self, mal_id):
        try:
            if self._episodes is None and not await self.ping(): return []
            cursor = self._episodes.find({"mal_id": mal_id}).sort([("season", 1), ("episode", 1)])
            docs = await cursor.to_list(length=1000)
            return clean_doc(docs) or []
        except Exception as e:
            logger.error(f"Read Error (get_episodes): {e}")
            return []

    async def get_all_categories(self):
        """Return explicit categories plus categories referenced by anime pages."""
        try:
            names = {}
            if self._anime is None or self._categories is None:
                await self.ping()
            if self._categories is not None:
                docs = await self._categories.find().to_list(length=1000)
                for doc in docs or []:
                    name = str(doc.get("name", "")).strip()
                    if name: names[name.casefold()] = {**clean_doc(doc), "name": name}
            if self._anime is not None:
                # Legacy projects often stored category only on anime documents.
                values = await self._anime.distinct("category")
                for value in values or []:
                    name = str(value or "").strip()
                    if name and name.casefold() not in names:
                        names[name.casefold()] = {"name": name}
            return sorted(names.values(), key=lambda item: item["name"].casefold())
        except Exception as e:
            logger.error("Read Error (get_all_categories): %s", e)
            return []

    async def get_setting(self, key, default=None):
        try:
            if self._settings is None and not await self.ping(): return default
            doc = await self._settings.find_one({"key": key})
            return doc.get("value", default) if doc else default
        except Exception as e:
            logger.error("Read Error (get_setting %s): %s", key, e)
            return default

    async def set_setting(self, key, value):
        try:
            if self._settings is None and not await self.ping(): return None
            return await self._settings.update_one(
                {"key": key}, {"$set": {"key": key, "value": value}}, upsert=True
            )
        except Exception as e:
            logger.error("Persistence Error (set_setting %s): %s", key, e)
            return None

    async def add_category(self, name):
        try:
            if self._categories is None and not await self.ping(): return None
            return await self._categories.update_one({"name": name}, {"$set": {"name": name}}, upsert=True)
        except Exception as e:
            logger.error(f"Persistence Error (add_category): {e}")
            return None

    async def delete_category(self, name):
        try:
            if self._categories is None and not await self.ping(): return None
            return await self._categories.delete_one({"name": name})
        except Exception as e:
            logger.error(f"Persistence Error (delete_category): {e}")
            return None

    async def update_schedule(self, day, content):
        try:
            if self._schedules is None: return None
            return await self._schedules.update_one({"day": day}, {"$set": {"content": content}}, upsert=True)
        except Exception as e:
            logger.error(f"Persistence Error (update_schedule): {e}")
            return None

    async def get_schedule(self, day):
        try:
            if self._schedules is None: return []
            res = await self._schedules.find_one({"day": day})
            if not res: return []

            content = res.get("content", [])
            # Convert string to structured list and extract URLs
            if isinstance(content, str):
                structured = []
                for line in content.split("\n"):
                    line = line.strip()
                    if not line: continue

                    image_url = None
                    time_val = "TBA"

                    # Extract URL (more robustly, looking for common image extensions too)
                    url_match = re.search(r'(https?://[^\s]+\.(?:jpg|jpeg|png|webp|gif|bmp)(?:\?[^\s]*)?)', line, re.I)
                    if not url_match:
                        # Fallback to any URL
                        url_match = re.search(r'(https?://[^\s]+)', line)

                    if url_match:
                        image_url = url_match.group(0).strip('.,()[]{}')
                        line = line.replace(url_match.group(0), "").strip()

                    # Extract Time (e.g. "12:00 PM", "(12:00)", "12.00")
                    time_match = re.search(r'\(?(\d{1,2}[:.]\d{2}\s*(?:AM|PM|am|pm)?)\)?', line)
                    if time_match:
                        time_val = time_match.group(1).replace('.', ':')
                        line = line.replace(time_match.group(0), "").strip()

                    # Clean up remaining name (remove leading dots/numbers if any)
                    name = re.sub(r'^[\d\.\-\s]+', '', line).strip()
                    if not name and image_url: name = "Untitled Entry"

                    if name or image_url:
                        structured.append({"name": name, "time": time_val, "image": image_url})
                return structured
            return content
        except Exception as e:
            logger.error(f"Read Error (get_schedule): {e}")
            return []

    # --- Background Songs Management ---

    async def add_song(self, data):
        try:
            if self._songs is None: return None
            # If song_id provided, update or insert
            song_id = data.get("song_id")
            if not song_id:
                song_id = str(ObjectId())
                data["song_id"] = song_id
            res = await self._songs.update_one({"song_id": song_id}, {"$set": data}, upsert=True)
            return song_id
        except Exception as e:
            logger.error(f"Persistence Error (add_song): {e}")
            return None

    async def get_all_songs(self):
        try:
            if self._songs is None: return []
            cursor = self._songs.find().sort([("created_at", -1), ("_id", -1)])
            docs = await cursor.to_list(length=1000)
            return clean_doc(docs) or []
        except Exception as e:
            logger.error(f"Read Error (get_all_songs): {e}")
            return []

    async def get_song(self, song_id):
        try:
            if self._songs is None: return None
            doc = await self._songs.find_one({"song_id": song_id})
            if not doc and len(song_id) == 24:
                try:
                    doc = await self._songs.find_one({"_id": ObjectId(song_id)})
                except: pass
            return clean_doc(doc)
        except Exception as e:
            logger.error(f"Read Error (get_song): {e}")
            return None

    async def delete_song(self, song_id):
        try:
            if self._songs is None: return None
            res = await self._songs.delete_one({"song_id": song_id})
            if res.deleted_count == 0 and len(song_id) == 24:
                try:
                    res = await self._songs.delete_one({"_id": ObjectId(song_id)})
                except: pass
            return res
        except Exception as e:
            logger.error(f"Persistence Error (delete_song): {e}")
            return None

    async def replace_song(self, song_id, data):
        try:
            if self._songs is None: return None
            data["song_id"] = song_id
            return await self._songs.update_one({"song_id": song_id}, {"$set": data}, upsert=True)
        except Exception as e:
            logger.error(f"Persistence Error (replace_song): {e}")
            return None

    async def set_song_channel(self, channel_id):
        try:
            if self._settings is None: return None
            return await self._settings.update_one({"key": "song_channel"}, {"$set": {"key": "song_channel", "value": channel_id}}, upsert=True)
        except Exception as e:
            logger.error(f"Persistence Error (set_song_channel): {e}")
            return None

    async def get_song_channel(self):
        try:
            if self._settings is None: return None
            doc = await self._settings.find_one({"key": "song_channel"})
            if doc:
                return doc.get("value")
            return None
        except Exception as e:
            logger.error(f"Read Error (get_song_channel): {e}")
            return None

    # --- Post Channel Configuration ---

    async def set_post_channel(self, channel_id):
        try:
            if self._settings is None: return None
            clean_val = str(channel_id).strip()
            return await self._settings.update_one(
                {"key": "post_channel"},
                {"$set": {"key": "post_channel", "value": clean_val}},
                upsert=True
            )
        except Exception as e:
            logger.error(f"Persistence Error (set_post_channel): {e}")
            return None

    async def get_post_channel(self):
        try:
            if self._settings is None: return None
            doc = await self._settings.find_one({"key": "post_channel"})
            if doc:
                return doc.get("value")
            return None
        except Exception as e:
            logger.error(f"Read Error (get_post_channel): {e}")
            return None

    # --- Configured Bot & Session Settings ---

    async def add_configured_bot(self, username: str):
        try:
            if self._settings is None: return None
            clean_username = username.strip().lstrip("@")
            doc = await self._settings.find_one({"key": "configured_bots"})
            bots = doc.get("value", []) if doc and isinstance(doc.get("value"), list) else []

            # Case-insensitive uniqueness check
            if not any(b.lower() == clean_username.lower() for b in bots):
                bots.append(clean_username)

            await self._settings.update_one(
                {"key": "configured_bots"},
                {"$set": {"key": "configured_bots", "value": bots}},
                upsert=True
            )
            # Also set configured_bot fallback
            await self._settings.update_one(
                {"key": "configured_bot"},
                {"$set": {"key": "configured_bot", "value": clean_username}},
                upsert=True
            )
            return True
        except Exception as e:
            logger.error(f"Persistence Error (add_configured_bot): {e}")
            return False

    async def set_configured_bot(self, username: str):
        return await self.add_configured_bot(username)

    async def get_configured_bots(self):
        try:
            if self._settings is None: return []
            doc = await self._settings.find_one({"key": "configured_bots"})
            if doc and isinstance(doc.get("value"), list):
                return doc.get("value")
            # Fallback to single bot if exists
            single_doc = await self._settings.find_one({"key": "configured_bot"})
            if single_doc and single_doc.get("value"):
                return [single_doc.get("value")]
            return []
        except Exception as e:
            logger.error(f"Read Error (get_configured_bots): {e}")
            return []

    async def get_configured_bot(self):
        bots = await self.get_configured_bots()
        return bots[0] if bots else None

    async def delete_configured_bot(self, username: str):
        try:
            if self._settings is None: return False
            clean_username = username.strip().lstrip("@")
            doc = await self._settings.find_one({"key": "configured_bots"})
            bots = doc.get("value", []) if doc and isinstance(doc.get("value"), list) else []

            updated_bots = [b for b in bots if b.lower() != clean_username.lower()]
            await self._settings.update_one(
                {"key": "configured_bots"},
                {"$set": {"key": "configured_bots", "value": updated_bots}},
                upsert=True
            )

            # Update single fallback
            fallback_val = updated_bots[0] if updated_bots else None
            await self._settings.update_one(
                {"key": "configured_bot"},
                {"$set": {"key": "configured_bot", "value": fallback_val}},
                upsert=True
            )
            return True
        except Exception as e:
            logger.error(f"Persistence Error (delete_configured_bot): {e}")
            return False

    async def set_configured_session(self, session_string: str):
        try:
            if self._settings is None: return None
            return await self._settings.update_one(
                {"key": "configured_session"},
                {"$set": {"key": "configured_session", "value": session_string.strip()}},
                upsert=True
            )
        except Exception as e:
            logger.error(f"Persistence Error (set_configured_session): {e}")
            return None

    async def get_configured_session(self):
        try:
            if self._settings is None: return None
            doc = await self._settings.find_one({"key": "configured_session"})
            if doc:
                return doc.get("value")
            return None
        except Exception as e:
            logger.error(f"Read Error (get_configured_session): {e}")
            return None

    async def is_admin(self, user_id):
        """Check admin records written by both old string-ID and numeric-ID builds."""
        try:
            if self._users is None:
                return False
            normalized_id = int(str(user_id).strip())
            user = await self._users.find_one({
                "user_id": {"$in": [normalized_id, str(normalized_id)]},
                "is_admin": True,
            })
            return user is not None
        except Exception as exc:
            logger.warning("Database admin lookup failed: %s", exc)
            return False

    async def export_data(self):
        try:
            if self._db is None: return None
            data = {}
            collections = {
                "anime": self._anime,
                "episodes": self._episodes,
                "users": self._users,
                "categories": self._categories,
                "schedules": self._schedules
            }
            total_found = 0
            for name, coll in collections.items():
                if coll is not None:
                    docs = await coll.find().to_list(length=100000)
                    data[name] = clean_doc(docs)
                    total_found += len(docs)

            if total_found == 0:
                return {} # Return empty dict to indicate connected but empty
            return data
        except Exception as e:
            logger.error(f"Export Error: {e}")
            return None

    async def import_data(self, data):
        try:
            if self._db is None: return False
            collections = {
                "anime": self._anime,
                "episodes": self._episodes,
                "users": self._users,
                "categories": self._categories,
                "schedules": self._schedules
            }

            # Prepare data and validate before deleting
            to_import = {}
            for name, docs in data.items():
                coll = collections.get(name)
                if coll is not None and isinstance(docs, list):
                    processed_docs = []
                    for doc in docs:
                        if isinstance(doc, dict):
                            # Handle MongoDB ObjectId if present in string format
                            if "_id" in doc and isinstance(doc["_id"], str) and len(doc["_id"]) == 24:
                                try: doc["_id"] = ObjectId(doc["_id"])
                                except: pass
                            processed_docs.append(doc)
                    to_import[name] = processed_docs

            # Now perform deletions and insertions in a transaction-like manner
            for name, coll in collections.items():
                if name in to_import:
                    await coll.delete_many({})
                    if to_import[name]:
                        await coll.insert_many(to_import[name])

            return True
        except Exception as e:
            logger.error(f"Import Error: {e}")
            return False

db = Database()
