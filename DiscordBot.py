import asyncio
import json
import os
import traceback

import discord
from discord.ext import tasks

from Variables import Constants
from Modules.AmazonScraper import AmazonScraper
from Modules.DealRouter import DealRouter
from Notification.DatabaseHandler import DatabaseHandler
import Modules.Helper as Helper
from Components.Pagination.PaginationView import Pagination
from Components.Pagination.PaginationSchedulerView import PaginationScheduler
from Components.RemoveFilterDropdown.View import NotificationRemoveView

NOTIFICATION_INTERVAL = float(os.environ.get("NOTIFICATION_INTERVAL", "21600"))
DEAL_SCAN_INTERVAL = float(os.environ.get("DEAL_SCAN_INTERVAL", "1800"))
DEAL_SCAN_MAX_PAGES = int(os.environ.get("DEAL_SCAN_MAX_PAGES", "10"))
DEAL_MAX_CODES_PER_SCAN = int(os.environ.get("DEAL_MAX_CODES_PER_SCAN", "5"))
DEAL_CODE_FETCH_DELAY = float(os.environ.get("DEAL_CODE_FETCH_DELAY", "8"))

async def mandatory_check(ctx):
    allowed = await Notification.get_whitelist(True)
    disallowed = await Notification.get_blacklist(True)

    if str(ctx.channel.id) not in allowed: return "This command is not allowed to be used in this channel!"
    if ctx.guild is None: return "This command can only be used in a server!"
    if str(ctx.guild.id) in disallowed: return "This guild has been blacklisted from using this bot. Please contact support if you believe this is a mistake."
    if Constants.MAINTENANCE and ctx.author.id not in Constants.SUPPORT_USERS: return "Bot is currently in maintenance mode. Please try again later."
    return None

@tasks.loop(seconds=3600.0)
async def regularly_check():
    print("Starting regular check")

    whitelisted = await Notification.get_whitelist(True)
    disallowed = await Notification.get_blacklist(True)

    for guild in bot.guilds:
        if not Helper.guild_has_support(guild):
            await Notification.add_blacklist(guild.id)
            for channel in guild.channels:
                if str(channel.id) in whitelisted:
                    bot.loop.create_task(channel.send(
                        "This guild has been blacklisted from using this bot. Please contact support if you believe this is a mistake."))
                    break
        else:
            if str(guild.id) in disallowed:
                await Notification.remove_blacklist(guild.id)

init = True

scraper = AmazonScraper(Constants.TESSERACT_LOCATION, proxy=Constants.PROXY)
Notification = DatabaseHandler()
deal_router = None

categories = list(scraper.categories.keys())
categories.append("all")

try:
    with open("data/cookies.txt", "r") as f:
        accounts = f.read().strip().split("\n")
    for account in accounts:
        if account.strip():
            scraper.load_account(json.loads(account))
    scraper.rotate_accounts()
except FileNotFoundError:
    print("[Startup] WARNING: data/cookies.txt not found — coupon fetching disabled")
except json.JSONDecodeError as e:
    print(f"[Startup] ERROR: Failed to parse cookies.txt: {e}")

intents = discord.Intents.all()
bot = discord.Bot(intents=intents)


async def log_error(message, error=None):
    """Log an error to the error channel if configured, otherwise just print."""
    error_text = f"**[Error]** {message}"
    if error:
        tb = traceback.format_exception(type(error), error, error.__traceback__)
        error_text += f"\n```\n{''.join(tb[-3:])}```"
    print(error_text)
    if Constants.ERROR_CHANNEL:
        try:
            ch = bot.get_channel(Constants.ERROR_CHANNEL)
            if ch:
                await ch.send(error_text[:2000])
        except Exception:
            pass


@bot.event
async def on_ready():
    global deal_router
    deal_router = DealRouter(bot, Notification)

    if Constants.MAINTENANCE:
        await bot.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching, name=" new features come to life! (Maintenance Mode)"
            )
        )
    else:
        await bot.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.playing, name="with amazon deals!"
            )
        )
    print(f'We have logged in as {bot.user}')

    await Notification.ensure_indexes()

    Notification_Routine.start()
    Deal_Routine.start()
    print(f"[DealRouter] Scan routine started (interval={int(DEAL_SCAN_INTERVAL)}s, max_pages={DEAL_SCAN_MAX_PAGES})")

    if not Constants.OVERRIDE_BLACKLIST:
        await regularly_check.start()


# ─── Deal Scan Routine ─────────────────────────────────────────────

@tasks.loop(seconds=DEAL_SCAN_INTERVAL)
async def Deal_Routine():
    routes = await Notification.get_all_deal_routes()
    if not routes:
        return

    print("[DealRouter] Starting deal scan")
    posted_count = 0
    codes_fetched = 0
    code_fetch_blocked = False

    try:
        page = 1
        while page <= DEAL_SCAN_MAX_PAGES:
            coupons = await bot.loop.run_in_executor(
                None, scraper.get_coupons_search, "", "", "", "", "newest", "", page
            )

            if coupons.get("status") != "success":
                print(f"[DealRouter] API error: {coupons.get('message', '?')} (code={coupons.get('code', '?')})")
                break

            if not coupons.get("data"):
                break

            parsed = await bot.loop.run_in_executor(None, scraper.parse_search, coupons["data"])

            if not parsed:
                break

            for listing in parsed.values():
                deal_id = str(listing.get("id", ""))
                if not deal_id or deal_id == "-1":
                    continue

                if await Notification.is_deal_posted(deal_id):
                    continue

                discount_pct = deal_router.parse_discount(listing.get("discount", "0"))
                matching = await Notification.get_matching_deal_routes(discount_pct)
                if not matching:
                    continue

                can_fetch = (
                    scraper.current is not None
                    and not code_fetch_blocked
                    and codes_fetched < DEAL_MAX_CODES_PER_SCAN
                )

                if can_fetch:
                    try:
                        code = await bot.loop.run_in_executor(None, scraper.get_code, deal_id)
                        if code == "rate_limited":
                            print(f"[DealRouter] Cloudflare rate-limited — skipping codes for rest of scan")
                            code_fetch_blocked = True
                        elif isinstance(code, str) and code not in ("This shouldn't of happened!",):
                            listing["coupon_code"] = code
                            codes_fetched += 1
                        await asyncio.sleep(DEAL_CODE_FETCH_DELAY)
                    except Exception as e:
                        print(f"[DealRouter] Code fetch failed for {deal_id}: {e}")

                count = await deal_router.post_deal_to_routes(listing)
                if count > 0:
                    await Notification.mark_deal_posted(deal_id)
                    posted_count += 1

            page += 1
            await asyncio.sleep(1)

    except Exception as e:
        await log_error("Deal scan routine failed", e)

    print(f"[DealRouter] Scan complete — posted {posted_count} new deal(s), fetched {codes_fetched} code(s)")


# ─── DM Notification Routine ───────────────────────────────────────

@tasks.loop(seconds=NOTIFICATION_INTERVAL)
async def Notification_Routine():

    disallowed = await Notification.get_blacklist(True)

    print("Starting Notification Routine")

    all_users = await Notification.get_all_users()

    if not all_users:
        return

    for user_data in all_users:
        try:
            if str(user_data["guild"]) in disallowed:
                bot.loop.create_task(bot.get_user(user_data["user"]).send("The guild you were in has been blacklisted from using this bot. Your notification task will not be fulfilled due to this. Please contact support if you believe this is a mistake."))
                continue
            if user_data["filters"] is None:
                continue
            await process_user_data(user_data)
        except Exception as e:
            await log_error(f"Failed processing user {user_data.get('user', '?')}", e)

async def send_notification(user_id, username, filter_embed):
    await bot.get_user(user_id).send(
        content=f"Hey {username}! It's me! The Amazon Deal Notification System!",
        embed=filter_embed
    )

async def process_user_data(user_data):
    user_id = user_data["user"]
    username = user_data["name"]
    filters = user_data["filters"]
    already_checked = user_data["already_checked"]

    filter_embed = Helper.create_filter_embed(len(filters))

    await send_notification(user_id, username, filter_embed)

    for filter_data in filters:
        index = filters.index(filter_data)

        to_send_to_user = await process_filter(user_id, filter_data, already_checked, index)

        if to_send_to_user:
            embed_to_store = await create_filter_store_embed(user_id, filter_data, filters.index(filter_data))
            listing_embed = Helper.create_listing_embed(to_send_to_user[0])

            await send_listing_notification(user_id, listing_embed, embed_to_store, to_send_to_user)
        else:
            await send_no_listings_notification(user_id)


async def process_filter(user_id, filter_data, already_checked, index):
    to_send_to_user = []
    inner_page = 1

    already_checked = already_checked[index]

    while True:
        listings = scraper.get_coupons_search(filter_data["search"], filter_data["fulfillment"],
                                              filter_data["discount"], filter_data["category"],
                                              filter_data["sorting"], filter_data["price"], inner_page)

        if listings.get("status") != "success" or not listings.get("data"):
            break

        parsed = scraper.parse_search(listings["data"])

        if not parsed:
            break

        for listing in parsed.values():
            if listing["id"] not in already_checked and listing["id"] != -1:
                to_send_to_user.append(listing)
                await Notification.add_already_checked(user_id, index, listing["id"])

        if inner_page > 50:
            break

        inner_page += 1

    return to_send_to_user


async def send_listing_notification(user_id, listing_embed, embed_to_store, scraped_data):
    await bot.get_user(user_id).send(
        content=f"Wake up! Found some new goodies for you :)\n\n{Constants.TIP}",
        embed=listing_embed,
        view=PaginationScheduler(scraper, scraped_data, bot, embed_to_store)
    )


async def send_no_listings_notification(user_id):
    sad_embed = Helper.create_sad_embed()
    await bot.get_user(user_id).send(embed=sad_embed)

async def create_filter_store_embed(user_id, filter_data, the_index):
    human_readable_filter = await Notification.get_filter_by_index(user_id, True, the_index)

    embed_to_store = discord.Embed(
        title=f"Filter {the_index + 1} | Amazon Deal Notification System"
    )

    embed_to_store.color = discord.Color.random()

    embed_to_store.add_field(name="Search", value=human_readable_filter["search"], inline=True)
    embed_to_store.add_field(name="Fulfillment", value=human_readable_filter["fulfillment"], inline=True)
    embed_to_store.add_field(name="Discount", value=human_readable_filter["discount"], inline=True)
    embed_to_store.add_field(name="Category", value=human_readable_filter["category"], inline=True)
    embed_to_store.add_field(name="Sorting", value=human_readable_filter["sorting"], inline=True)

    if human_readable_filter["price"] == "No preference":
        embed_to_store.add_field(name="Price Beginning", value="No preference", inline=True)
        embed_to_store.add_field(name="Price End", value="No preference", inline=True)
    else:
        embed_to_store.add_field(name="Price Beginning", value=human_readable_filter["price_beginning"],
                                 inline=True)
        embed_to_store.add_field(name="Price End", value=human_readable_filter["price_end"], inline=True)

    embed_to_store.set_footer(text=f"Developed by {Constants.AUTHOR_NAME}")

    return embed_to_store

@bot.event
async def on_application_command_error(ctx: discord.ApplicationContext, error: discord.DiscordException):
    if isinstance(error, discord.Forbidden):
        await ctx.respond("I was unable to send a message to you. Please check your privacy settings.", ephemeral=True)
    else:
        await log_error(f"Command error in /{ctx.command.qualified_name if ctx.command else '?'}", error)
        raise error

@bot.command(description="Clear someone's already checked stuff", guild_ids=Constants.SUPPORT_GUILD)
async def admin_clear_already_checked(
    ctx,
    user_id: discord.Option(str, description="Who am I addressing?", required=True),

):
    await ctx.defer(ephemeral=True)

    if ctx.author.id not in Constants.SUPPORT_USERS:
        await ctx.interaction.followup.send("You are not authorized to use this command!", ephemeral=True)
        return

    await ctx.interaction.followup.send(await Notification.clear_already_checked(user_id), ephemeral=True)

@bot.command(description="Force restart the notification routine", guild_ids=Constants.SUPPORT_GUILD)
async def admin_force_restart(
    ctx
):
    await ctx.defer(ephemeral=True)

    if ctx.author.id not in Constants.SUPPORT_USERS:
        await ctx.interaction.followup.send("You are not authorized to use this command!", ephemeral=True)
        return

    if Notification_Routine.is_running():
        Notification_Routine.cancel()


    try:
        Notification_Routine.restart()
    except Exception as e:
        await ctx.interaction.followup.send(f"Something went wrong!\n\n{e}", ephemeral=True)

    await ctx.interaction.followup.send("Successfully restarted the notification routine!", ephemeral=True)

@bot.command(description="Add a channel to whitelist", guild_ids=Constants.SUPPORT_GUILD)
async def add_whitelist(
    ctx,
    channel: discord.Option(str, description="Channel to whitelist", required=True)
):
    await ctx.defer(ephemeral=True)

    if ctx.author.id not in Constants.SUPPORT_USERS:
        await ctx.interaction.followup.send("You are not authorized to use this command!", ephemeral=True)
        return

    toDo = await Notification.add_channel(channel)
    await ctx.interaction.followup.send(toDo, ephemeral=True)

@bot.command(description="Remove a channel from whitelist", guild_ids=Constants.SUPPORT_GUILD)
async def remove_whitelist(
    ctx,
    channel: discord.Option(str, description="Channel to remove from whitelist", required=True)
):
    await ctx.defer(ephemeral=True)

    if ctx.author.id not in Constants.SUPPORT_USERS:
        await ctx.interaction.followup.send("You are not authorized to use this command!", ephemeral=True)
        return

    toDo = await Notification.remove_channel(channel)
    await ctx.interaction.followup.send(toDo, ephemeral=True)


@bot.command(description="Get whitelist", guild_ids=Constants.SUPPORT_GUILD)
async def get_whitelist(
    ctx
):
    await ctx.defer(ephemeral=True)

    if ctx.author.id not in Constants.SUPPORT_USERS:
        await ctx.interaction.followup.send("You are not authorized to use this command!", ephemeral=True)
        return

    whitelist = await Notification.get_whitelist()
    await ctx.interaction.followup.send(whitelist, ephemeral=True)

@bot.command(description="Get blacklist", guild_ids=Constants.SUPPORT_GUILD)
async def get_blacklist(
    ctx
):
    await ctx.defer(ephemeral=True)

    if ctx.author.id not in Constants.SUPPORT_USERS:
        await ctx.interaction.followup.send("You are not authorized to use this command!", ephemeral=True)
        return

    blacklist = await Notification.get_blacklist()
    await ctx.interaction.followup.send(blacklist, ephemeral=True)

@bot.command(description="Add a guild to blacklist", guild_ids=Constants.SUPPORT_GUILD)
async def add_blacklist(
    ctx,
    guild: discord.Option(str, description="Guild to blacklist", required=True)
):
    await ctx.defer(ephemeral=True)

    if ctx.author.id not in Constants.SUPPORT_USERS:
        await ctx.interaction.followup.send("You are not authorized to use this command!", ephemeral=True)
        return

    toDo = await Notification.add_blacklist(guild)
    await ctx.interaction.followup.send(toDo, ephemeral=True)

@bot.command(description="Remove a guild from blacklist", guild_ids=Constants.SUPPORT_GUILD)
async def remove_blacklist(
    ctx,
    guild: discord.Option(str, description="Guild to remove from blacklist", required=True)
):
    await ctx.defer(ephemeral=True)

    if ctx.author.id not in Constants.SUPPORT_USERS:
        await ctx.interaction.followup.send("You are not authorized to use this command!", ephemeral=True)
        return

    toDo = await Notification.remove_blacklist(guild)
    await ctx.interaction.followup.send(toDo, ephemeral=True)

@bot.command(description="Find Amazon Deals without needing keywords!")
async def search_without_keywords(
    ctx,
    fulfillment: discord.Option(str, description="Fulfillment Type", choices=["merchant", "amazon", "all"], default="all"),
    discount: discord.Option(str, description="Discount Type (Percentage)", choices=["all", "20-49", "50-79", "80-101"], default="all"),
    category: discord.Option(str, description="Category", choices=categories, default="all"),
    sorting: discord.Option(str, description="Sorting", choices=["No preference", "Low to High", "High to Low", "Discount High to Low", "Newest"], default="No preference"),
    price_beginning: discord.Option(int, min_value=0, description="Price Range Beginning", default=None),
    price_end: discord.Option(int, max_value=9999999, description="Price Range End", default=None)
):
    await ctx.defer(ephemeral=True)

    if Constants.LOG_CHANNEL:
        log_channel = bot.get_channel(Constants.LOG_CHANNEL)
        if log_channel:
            await log_channel.send(Helper.get_command_log_message_without(ctx, fulfillment, discount, category, sorting, price_beginning, price_end))

    check_result = await mandatory_check(ctx)
    if check_result is not None:
        await ctx.interaction.followup.send(check_result, ephemeral=True)
        return

    fulfillment = Helper.map_fulfillment(fulfillment)
    discount = Helper.map_discount(discount)
    sorting = Helper.map_sorting(sorting)
    category = Helper.map_category(category)

    price = Helper.map_price(price_beginning, price_end)

    page = 1

    coupons_data = await bot.loop.run_in_executor(None, scraper.get_coupons, fulfillment, discount, category, sorting, price, page)

    if coupons_data.get("status") != "success":
        print(f"[search_without_keywords] API error: {coupons_data}")
        await ctx.interaction.followup.send(
            f"Something went wrong! The deal API returned: {coupons_data.get('code', 'unknown')}",
            ephemeral=True,
        )
        return

    if not coupons_data["data"]:
        await ctx.interaction.followup.send("No results found!", ephemeral=True)
        return

    scraped = await bot.loop.run_in_executor(None, scraper.parse, coupons_data["data"])

    da_embed = Helper.create_listing_embed_generic(scraped[0])

    await ctx.interaction.followup.send(
        f"You are currently on deal *1* out of **{len(scraped)}**\n{Constants.TIP}",
        embed=da_embed,
        view=Pagination(ctx, scraped, fulfillment, discount, category, sorting, price, scraper, bot, None),
        ephemeral=True
    )

@bot.command(description="Find Amazon Deals based on keywords! (Preferred)")
async def search_with_keywords(
        ctx,
        search: discord.Option(str, description="What to search?!", required=True),
        fulfillment: discord.Option(str, description="Fulfillment Type", choices=["merchant", "amazon", "all"],
                                    default="all"),
        discount: discord.Option(str, description="Discount Type (Percentage)",
                                 choices=["all", "20-49", "50-79", "80-101"], default="all"),
        category: discord.Option(str, description="Category", choices=categories, default="all"),
        sorting: discord.Option(str, description="Sorting",
                                choices=["No preference", "Low to High", "High to Low", "Discount High to Low",
                                         "Newest"], default="No preference"),
        price_beginning: discord.Option(int, min_value=1, description="Price Range Beginning", default=None),
        price_end: discord.Option(int, max_value=9999999, description="Price Range End", default=None)
):
    await ctx.defer(ephemeral=True)

    if Constants.LOG_CHANNEL:
        log_channel = bot.get_channel(Constants.LOG_CHANNEL)
        if log_channel:
            await log_channel.send(
                f"**{ctx.author}** used the command **/{ctx.command.qualified_name}** in channel **{ctx.channel}** with the following options:\nSearch: **{search}**\nFulfillment: **{fulfillment}**\nDiscount: **{discount}**\nCategory: **{category}**\nSorting: **{sorting}**\nPrice Beginning: **{price_beginning}**\nPrice End: **{price_end}**")

    check = await mandatory_check(ctx)
    if check is not None:
        await ctx.interaction.followup.send(check, ephemeral=True)
        return

    comment = ""

    fulfillment = Helper.map_fulfillment(fulfillment)
    discount = Helper.map_discount(discount)
    sorting = Helper.map_sorting(sorting)
    category = Helper.map_category(category)

    price = Helper.map_price(price_beginning, price_end)

    search = search or ""

    page = 1

    coupons_data = await bot.loop.run_in_executor(None, scraper.get_coupons_search, search, fulfillment, discount,
                                                  category, sorting, price, page)

    if not coupons_data["data"]:
        await ctx.interaction.followup.send("No results found!", ephemeral=True)
        return

    if coupons_data["status"] != "success":
        print(coupons_data)
        await ctx.respond("Something went wrong! Please report this.", ephemeral=True)
        return

    scraped = await bot.loop.run_in_executor(None, scraper.parse_search, coupons_data["data"])

    if not scraped:
        await ctx.interaction.followup.send("No results found!", ephemeral=True)
        return

    da_embed = Helper.create_listing_embed(scraped[0])

    await ctx.interaction.followup.send(
        f"You are currently on deal *1* out of **{len(scraped)}**\n{Constants.TIP}" + comment,
        embed=da_embed,
        view=Pagination(ctx, scraped, fulfillment, discount, category, sorting, price, scraper, bot, search),
        ephemeral=True)


@bot.command(description="Add a notification filter!")
async def add_filter(
    ctx,
    search: discord.Option(str, description="What to search?!", required=True),
    fulfillment: discord.Option(
        str, description="Fulfillment Type", choices=["merchant", "amazon", "all"], default="all"
    ),
    discount: discord.Option(
        str, description="Discount Type (Percentage)", choices=["all", "20-49", "50-79", "80-101"], default="all"
    ),
    category: discord.Option(str, description="Category", choices=categories, default="all"),
    sorting: discord.Option(
        str,
        description="Sorting",
        choices=["No preference", "Low to High", "High to Low", "Discount High to Low", "Newest"],
        default="No preference",
    ),
    price_beginning: discord.Option(int, min_value=1, description="Price Range Beginning", default=None),
    price_end: discord.Option(int, max_value=9999999, description="Price Range End", default=None),
):
    await ctx.defer(ephemeral=True)

    if Constants.LOG_CHANNEL:
        log_channel = bot.get_channel(Constants.LOG_CHANNEL)
        if log_channel:
            await log_channel.send(Helper.get_command_log_message_search(ctx, search, fulfillment, discount, category, sorting, price_beginning, price_end))

    check_result = await mandatory_check(ctx)
    if check_result is not None:
        await ctx.interaction.followup.send(check_result, ephemeral=True)
        return


    if len(await Notification.get_filters(ctx.author.id)) >= Notification.MAX_FILTERS:
        await ctx.interaction.followup.send(f"You already have {Notification.MAX_FILTERS} filters!", ephemeral=True)
        return

    fulfillment = Helper.map_fulfillment(fulfillment)
    discount = Helper.map_discount(discount)
    sorting = Helper.map_sorting(sorting)
    category = Helper.map_category(category)
    price = Helper.map_price(price_beginning, price_end)

    filter = {
        "search": search,
        "fulfillment": fulfillment,
        "discount": discount,
        "category": category,
        "sorting": sorting,
        "price": price,
    }

    if not await Notification.check_user_exists(ctx.author.id):
        await Notification.add_user(ctx.author.id, ctx.author.name, ctx.guild.id)

    if not await Notification.add_filter(ctx.author.id, filter):
        await ctx.interaction.followup.send("You already have this filter!", ephemeral=True)
        return

    await ctx.interaction.followup.send("Successfully added filter!", ephemeral=True)

@bot.command(description="Get a list of your filters!")
async def list_filters(ctx):
    await ctx.defer(ephemeral=True)

    if Constants.LOG_CHANNEL:
        log_channel = bot.get_channel(Constants.LOG_CHANNEL)
        if log_channel:
            await log_channel.send(
                f"**{ctx.author}** used the command **/{ctx.command.qualified_name}** in channel **{ctx.channel}**")

    check = await mandatory_check(ctx)
    if check:
        await ctx.interaction.followup.send(check, ephemeral=True)
        return

    if not await Notification.check_user_exists(ctx.author.id):
        embed = discord.Embed(title="You have no filters!", description="Add one using **/amazon notifications add**")
    else:
        filters = await Notification.get_filters(ctx.author.id, True)

        if len(filters) == 0:
            embed = discord.Embed(title="You have no filters!", description="Add one using **/amazon notifications add**")
        else:
            embed = Helper.create_filters_embed(filters)

    embed.color = discord.Color.random()
    embed.set_footer(text=f"Developed by {Constants.AUTHOR_NAME}")

    await ctx.interaction.followup.send(embed=embed, ephemeral=True)

@bot.command(description="Remove a filter!")
async def remove_filters(ctx):
    global filters
    await ctx.defer(ephemeral=True)

    if Constants.LOG_CHANNEL:
        log_channel = bot.get_channel(Constants.LOG_CHANNEL)
        if log_channel:
            await log_channel.send(
                f"**{ctx.author}** used the command **/{ctx.command.qualified_name}** in channel **{ctx.channel}**")

    check = await mandatory_check(ctx)
    if check:
        await ctx.interaction.followup.send(check, ephemeral=True)
        return

    user_exists = await Notification.check_user_exists(ctx.author.id)
    if not user_exists:
        embed = discord.Embed(title="You have no filters!", description="Add one using **/amazon notifications add**")
    else:
        filters = await Notification.get_filters(ctx.author.id, True)
        if not filters:
            embed = discord.Embed(title="You have no filters!", description="Add one using **/amazon notifications add**")
        else:
            embed = Helper.create_filters_embed(filters)

    embed.color = discord.Color.random()
    embed.set_footer(text=f"Developed by {Constants.AUTHOR_NAME}")

    if not user_exists or not filters:
        await ctx.interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await ctx.interaction.followup.send(embed=embed, view=NotificationRemoveView(ctx, bot, Notification, filters, embed), ephemeral=True)



# ─── Deal Route Management ─────────────────────────────────────────

deal_route = bot.create_group("deal_route", "Manage automatic deal posting to channels")


@deal_route.command(description="Route deals to a channel based on discount range")
@discord.default_permissions(manage_channels=True)
async def add(
    ctx,
    channel: discord.Option(discord.TextChannel, description="Channel to post deals to"),
    min_discount: discord.Option(int, description="Minimum discount % (0-100)", min_value=0, max_value=100),
    max_discount: discord.Option(int, description="Maximum discount % (0-100)", min_value=0, max_value=100),
):
    await ctx.defer(ephemeral=True)

    if min_discount > max_discount:
        await ctx.interaction.followup.send(
            "Min discount can't be greater than max discount!", ephemeral=True
        )
        return

    perms = channel.permissions_for(ctx.guild.me)
    if not perms.send_messages or not perms.embed_links:
        await ctx.interaction.followup.send(
            f"I need **Send Messages** and **Embed Links** permissions in {channel.mention}!",
            ephemeral=True,
        )
        return

    result = await Notification.add_deal_route(
        guild_id=ctx.guild.id,
        channel_id=channel.id,
        min_discount=min_discount,
        max_discount=max_discount,
        created_by=ctx.author.id,
    )

    if result == "updated":
        await ctx.interaction.followup.send(
            f"Updated route for {channel.mention}: **{min_discount}–{max_discount}%** off deals",
            ephemeral=True,
        )
    else:
        await ctx.interaction.followup.send(
            f"Deals with **{min_discount}–{max_discount}%** off will now post to {channel.mention}",
            ephemeral=True,
        )


@deal_route.command(name="list", description="Show all deal routes for this server")
@discord.default_permissions(manage_channels=True)
async def list_routes(ctx):
    await ctx.defer(ephemeral=True)

    routes = await Notification.get_deal_routes(ctx.guild.id)

    if not routes:
        await ctx.interaction.followup.send(
            "No deal routes configured. Use `/deal_route add` to get started!",
            ephemeral=True,
        )
        return

    embed = discord.Embed(
        title="Deal Routes",
        description="Deals are automatically posted to these channels based on discount range.",
        color=0x2ECC71,
    )

    for route in routes:
        channel = ctx.guild.get_channel(route["channel_id"])
        ch_name = channel.mention if channel else f"(deleted channel {route['channel_id']})"
        embed.add_field(
            name=ch_name,
            value=f"**{route['min_discount']}–{route['max_discount']}%** off",
            inline=True,
        )

    await ctx.interaction.followup.send(embed=embed, ephemeral=True)


@deal_route.command(description="Stop posting deals to a channel")
@discord.default_permissions(manage_channels=True)
async def remove(
    ctx,
    channel: discord.Option(discord.TextChannel, description="Channel to stop posting to"),
):
    await ctx.defer(ephemeral=True)

    removed = await Notification.remove_deal_route(ctx.guild.id, channel.id)

    if removed:
        await ctx.interaction.followup.send(
            f"Removed deal route for {channel.mention}", ephemeral=True
        )
    else:
        await ctx.interaction.followup.send(
            f"No deal route found for {channel.mention}", ephemeral=True
        )


try:
    bot.run(Constants.TOKEN)
except KeyboardInterrupt:
    exit()
