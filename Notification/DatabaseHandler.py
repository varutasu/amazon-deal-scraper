import asyncio
import copy
import os
from datetime import datetime, timezone

import motor.motor_asyncio
from pymongo.errors import DuplicateKeyError
from Variables import Constants


class DatabaseHandler:
    def __init__(self):
        self.MAX_FILTERS = int(os.environ.get("MAX_FILTERS", "3"))

        uri = os.environ.get("MONGODB_URI")
        if uri:
            self.client = motor.motor_asyncio.AsyncIOMotorClient(uri)
        else:
            self.client = motor.motor_asyncio.AsyncIOMotorClient(Constants.HOST, Constants.PORT)
        self.client.get_io_loop = asyncio.get_running_loop
        self.db = self.client['AmazonDealScraper']
        self.collection = self.db['noti-pref']
        self.settings = self.db['settings']
        self.deal_posted = self.db['deal-posted']
        self.deal_routes = self.db['deal-routes']
        self.code_queue = self.db['code-queue']
        self.deals = self.db['deals']
        self.myvipon_accounts = self.db['myvipon_accounts']

    # --- Deal dedup ---

    async def is_deal_posted(self, deal_id):
        return await self.deal_posted.find_one({"deal_id": str(deal_id)}) is not None

    async def mark_deal_posted(self, deal_id):
        try:
            await self.deal_posted.insert_one({
                "deal_id": str(deal_id),
                "posted_at": datetime.now(timezone.utc),
            })
        except DuplicateKeyError:
            pass

    # --- Deal routes ---

    async def add_deal_route(self, guild_id, channel_id, min_discount, max_discount, created_by=None):
        existing = await self.deal_routes.find_one({
            "guild_id": guild_id, "channel_id": channel_id,
        })
        if existing:
            await self.deal_routes.update_one(
                {"guild_id": guild_id, "channel_id": channel_id},
                {"$set": {
                    "min_discount": min_discount,
                    "max_discount": max_discount,
                    "updated_at": datetime.now(timezone.utc),
                }},
            )
            return "updated"

        await self.deal_routes.insert_one({
            "guild_id": guild_id,
            "channel_id": channel_id,
            "min_discount": min_discount,
            "max_discount": max_discount,
            "created_by": created_by,
            "created_at": datetime.now(timezone.utc),
        })
        return "created"

    async def remove_deal_route(self, guild_id, channel_id):
        result = await self.deal_routes.delete_one({
            "guild_id": guild_id, "channel_id": channel_id,
        })
        return result.deleted_count > 0

    async def get_deal_routes(self, guild_id):
        routes = []
        async for doc in self.deal_routes.find({"guild_id": guild_id}):
            routes.append(doc)
        return routes

    async def get_all_deal_routes(self):
        routes = []
        async for doc in self.deal_routes.find():
            routes.append(doc)
        return routes

    async def get_matching_deal_routes(self, discount_pct):
        routes = []
        async for doc in self.deal_routes.find({
            "min_discount": {"$lte": discount_pct},
            "max_discount": {"$gte": discount_pct},
        }):
            routes.append(doc)
        return routes

    # --- Code fetch queue ---

    async def queue_code_fetch(self, deal_id, messages):
        """Queue a deal for background code fetching. messages = [(channel_id, message_id), ...]"""
        await self.code_queue.insert_one({
            "deal_id": str(deal_id),
            "messages": [{"channel_id": c, "message_id": m} for c, m in messages],
            "queued_at": datetime.now(timezone.utc),
        })

    async def dequeue_code_fetch(self):
        """Get and remove the oldest item from the code queue."""
        return await self.code_queue.find_one_and_delete({}, sort=[("queued_at", 1)])

    async def requeue_code_fetch(self, item):
        """Put a failed item back at the end of the queue."""
        item.pop("_id", None)
        item["queued_at"] = datetime.now(timezone.utc)
        await self.code_queue.insert_one(item)

    async def get_code_queue_size(self):
        return await self.code_queue.count_documents({})

    async def clear_code_queue(self):
        await self.code_queue.delete_many({})

    # --- Normalized deals (shared with website) ---

    async def upsert_deal(self, deal):
        """Insert or update a normalized deal document. Deduplicates by source + source_id."""
        await self.deals.update_one(
            {"source": deal["source"], "source_id": deal["source_id"]},
            {"$set": deal, "$setOnInsert": {"first_seen": datetime.now(timezone.utc)}},
            upsert=True,
        )

    async def update_deal_code(self, source, source_id, code):
        """Update the coupon_code for a deal after background code fetching."""
        update = {"coupon_code": code} if code else {"coupon_code": None, "coupon_type": "none"}
        await self.deals.update_one(
            {"source": source, "source_id": str(source_id)},
            {"$set": update},
        )

    async def get_active_deals(self, limit=50, skip=0, category=None, min_discount=0, source=None):
        """Query active deals for the website API."""
        query = {"active": True}
        if category:
            query["category"] = category
        if min_discount:
            query["discount_pct"] = {"$gte": min_discount}
        if source:
            query["source"] = source
        cursor = self.deals.find(query).sort("first_seen", -1).skip(skip).limit(limit)
        return await cursor.to_list(length=limit)

    async def get_deal_by_slug(self, slug):
        return await self.deals.find_one({"slug": slug, "active": True})

    async def get_deal_categories(self):
        """Return distinct category values for filtering."""
        return await self.deals.distinct("category", {"active": True})

    async def ensure_indexes(self):
        await self.deal_posted.create_index("deal_id", unique=True)
        await self.deal_posted.create_index("posted_at", expireAfterSeconds=604800)
        await self.deal_routes.create_index(
            [("guild_id", 1), ("channel_id", 1)], unique=True,
        )
        await self.code_queue.create_index("queued_at", expireAfterSeconds=7200)
        await self.deals.create_index([("source", 1), ("source_id", 1)], unique=True)
        await self.deals.create_index([("active", 1), ("first_seen", -1)])
        await self.deals.create_index([("active", 1), ("discount_pct", -1)])
        await self.deals.create_index("slug", unique=True, sparse=True)
        await self.deals.create_index("first_seen", expireAfterSeconds=2592000)
        await self.myvipon_accounts.create_index("email", unique=True)

    # --- MyVipon account cookies ---

    async def get_active_account_cookies(self):
        """Return list of cookie dicts for all active MyVipon accounts."""
        cursor = self.myvipon_accounts.find(
            {"status": "active", "cookies": {"$ne": {}}}
        )
        accounts = await cursor.to_list(length=100)
        return [acc["cookies"] for acc in accounts]

    # --- User management ---

    async def add_user(self, user_id, name, guild_id):
        try:
            await self.collection.insert_one({"user": user_id, "name": name, "guild": guild_id, "filters": [], "already_checked": []})
        except DuplicateKeyError:
            pass

    async def check_user_exists(self, user_id):
        doc = await self.collection.find_one({"user": user_id})
        return doc is not None

    async def get_filters(self, user_id, user_readable=False):
        document = await self.collection.find_one({"user": user_id})

        if not document or document["filters"] is None:
            return []

        toReturn = copy.deepcopy(document)

        if user_readable:
            for i, filter in enumerate(document["filters"]):
                for key, value in filter.items():
                    if not bool(value):
                        toReturn["filters"][i][key] = "No preference"
                    elif "price" in key:
                        if not bool(value):
                            toReturn["filters"][i]["price_beginning"] = "No preference"
                            toReturn["filters"][i]["price_end"] = "No preference"
                        else:
                            toReturn["filters"][i]["price_beginning"] = value.split("-")[0]
                            toReturn["filters"][i]["price_end"] = value.split("-")[1]

        return toReturn["filters"]

    async def get_filter_by_index(self, user_id, user_readable, index: int):
        filters = await self.get_filters(user_id, user_readable)
        if index >= len(filters):
            return False

        return filters[index]

    async def add_filter(self, user_id, filter):
        if len(await self.get_filters(user_id)) >= self.MAX_FILTERS:
            return False

        if filter in await self.get_filters(user_id):
            return False

        await self.collection.update_one({"user": user_id}, {"$push": {"filters": filter}})

        await self.collection.update_one({"user": user_id}, {"$push": {"already_checked": []}})

        return True

    async def remove_filter(self, user_id, filter):
        if filter not in await self.get_filters(user_id):
            return False

        index = await self.get_index_of_filter(user_id, filter)

        if index is False:
            return False

        await self.collection.update_one({"user": user_id}, {"$unset": {f"already_checked.{index}": 1}})
        await self.collection.update_one({"user": user_id}, {"$pull": {f"already_checked": None}})

        await self.collection.update_one({"user": user_id}, {"$pull": {"filters": filter}})

        return True

    async def remove_filter_by_index(self, user_id, index: int):
        filters = await self.get_filters(user_id)

        if index >= len(filters):
            return False

        remove = await self.remove_filter(user_id, filters[index])

        if remove:
            return True
        else:
            return False

    async def remove_all_filters(self, user_id):
        await self.collection.update_one({"user": user_id}, {"$set": {"filters": []}})
        await self.collection.update_one({"user": user_id}, {"$set": {"already_checked": []}})

    async def get_index_of_filter(self, user_id, filter):
        filters = await self.get_filters(user_id)

        if filter not in filters:
            return False

        return filters.index(filter)

    async def get_already_checked(self, user_id, filterIndex):
        document = await self.collection.find_one({"user": user_id})
        return document["already_checked"][filterIndex] if document else []

    async def already_checked(self, user_id, listing_id, filterIndex):
        return listing_id in await self.get_already_checked(user_id, filterIndex)

    async def add_already_checked(self, user_id, filterIndex, listing_id):
        await self.collection.update_one({"user": user_id}, {"$push": {f"already_checked.{filterIndex}": listing_id}})

    async def get_all_users(self):
        docs = []
        async for document in self.collection.find():
            docs.append(document)
        return docs

    async def get_user(self, user_id):
        e = await self.collection.find_one({"user": user_id})
        return e

    async def clear_already_checked(self, user_id):
        try:
            await self.collection.update_one({"user": user_id}, {"$set": {"filters": []}})
            await self.collection.update_one({"user": user_id}, {"$set": {"already_checked": []}})
            return "Successfully cleared!"
        except Exception as e:
            return e

    async def add_channel(self, channel_id):
        if await self.get_whitelist() == "Something went wrong!":
            await self.settings.insert_one({"whitelist": [], "blacklist": []})

        try:
            int(channel_id)
        except ValueError:
            return "Invalid channel ID!"

        if channel_id in await self.get_whitelist():
            return "Already in whitelist!"

        await self.settings.update_one({}, {"$push": {"whitelist": channel_id}})

        return "Successfully added!"

    async def remove_channel(self, channel_id):
        if await self.get_whitelist() == "Something went wrong!":
            await self.settings.insert_one({"whitelist": [], "blacklist": []})

        try:
            int(channel_id)
        except ValueError:
            return "Invalid channel ID!"

        if channel_id not in await self.get_whitelist():
            return "Not in whitelist!"

        await self.settings.update_one({}, {"$pull": {"whitelist": channel_id}})

        return "Successfully removed!"


    async def get_whitelist(self, returnList=False):
        try:
            document = await self.settings.find_one({})
        except Exception as e:
            print(e)
            return "Something went wrong!"

        if not document:
            return "Something went wrong!"

        if returnList:
            return document["whitelist"] if document else "Something went wrong!"

        if len(document["whitelist"]) == 0:
            return "No channels in whitelist!"

        return ", ".join(str(each) for each in document["whitelist"])

    async def add_blacklist(self, channel_id):
        if await self.get_blacklist() == "Something went wrong!":
            await self.settings.insert_one({"whitelist": [], "blacklist": []})

        try:
            int(channel_id)
        except ValueError:
            return "Invalid channel ID!"

        if str(channel_id) in await self.get_blacklist():
            return "Already in blacklist!"

        await self.settings.update_one({}, {"$push": {"blacklist": str(channel_id)}})

        return "Successfully added!"

    async def remove_blacklist(self, channel_id):
        if await self.get_blacklist() == "Something went wrong!":
            await self.settings.insert_one({"whitelist": [], "blacklist": []})

        try:
            int(channel_id)
        except ValueError:
            return "Invalid channel ID!"

        if str(channel_id) not in await self.get_blacklist():
            return "Not in blacklist!"

        await self.settings.update_one({}, {"$pull": {"blacklist": str(channel_id)}})
        return "Successfully removed!"

    async def get_blacklist(self, returnList=False):
        try:
            document = await self.settings.find_one({})
        except Exception as e:
            print(e)
            return "Something went wrong!"

        if not document:
            return "Something went wrong!"

        if len(document["blacklist"]) == 0:
            return "No guilds in blacklist!"

        if returnList:
            return document["blacklist"] if document else "Something went wrong!"
        return ", ".join(str(each) for each in document["blacklist"])
