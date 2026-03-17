import urllib.parse as urlparse

import bs4
import cv2
import numpy as np
import pytesseract
import random
import time
from curl_cffi import requests as curl_requests

class AmazonScraper:
    def __init__(self, path=None, proxy=False, code_fetch_proxy=None):
        if path is not None:
            pytesseract.pytesseract.tesseract_cmd = path

        self.base = "https://myvipon.com/"
        self.domain = "www.amazon.com"
        self.session = curl_requests.Session(impersonate="chrome")
        if proxy:
            self.session.proxies = {"https": proxy, "http": proxy}

        self.code_fetch_proxies = None
        if code_fetch_proxy:
            self.code_fetch_proxies = {"https": code_fetch_proxy, "http": code_fetch_proxy}
            print(f"[AmazonScraper] Code-fetch proxy: {code_fetch_proxy}")

        self.webhook = ""
        self.debug = False

        self.working = []
        self.limit = []
        self.current = None

        self.SUCCESSFUL = [
            "You've saved",
            "You have requested this code previously."
        ]

        self.FAILED = [
            "Invalid Request",
            "Please complete the equation below to continue.",
            "Oops, Instant vouchers have run out.."
        ]

        self.fulfillment = ""
        self.discount = ""
        self.status = ""
        self.priceRange = ""
        self.category = ""
        self.type = ""
        self.page = ""

        self.queue = []
        self.queue_running = False

        self.categories = {
            "Arts, Crafts & Sewing": "14",
            "Automotive & Industrial": "19",
            "Baby": "16",
            "Beauty & Personal Care": "5",
            "Cell Phones & Accessories": "11",
            "Electronics": "8",
            "Health & Household": "9",
            "Home & Kitchen": "1",
            "Jewelry": "4",
            "Men Clothing, Shoes & Accessories": "15",
            "Office Products": "18",
            "Patio, Lawn & Garden": "13",
            "Pet Supplies": "17",
            "Sports & Outdoors": "12",
            "Tools & Home Improvement": "7",
            "Toys & Games": "6",
            "Watches": "3",
            "Women Clothing, Shoes & Accessories": "2",
            "Others": "20",
            "Adult Products": "10"
        }

        self.sort = {
            "Default": "",
            "Low to High": "price",
            "High to Low": "price_contrary",
            "Discount High to Low": "discount",
            "Newest": "newest"
        }

        self.headers = {
            "Accept-Language": "en-US,en;q=0.5",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "https://myvipon.com",
            "DNT": "1",
            "Connection": "keep-alive",
            "Referer": "https://myvipon.com/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Pragma": "no-cache",
            "Cache-Control": "no-cache",
        }

        self.session.get("https://myvipon.com/")

    def handle_queue(self):
        while self.queue_running:
            if self.queue == []:
                time.sleep(1)
                continue
            else:
                idd = self.queue.pop(0)
                self.get_code(idd)
                time.sleep(random.randint(1, 3))
                continue

    def authenticate(self, username, password, recaptcha_token):
        username = urlparse.quote(username)
        password = urlparse.quote(password)
        print(f"email={username}&password={password}&google_recaptcha_token={recaptcha_token}")
        authReq = self.session.post("https://myvipon.com/api2/passport/login", headers=self.headers,
                                    data=f"email={username}&password={password}&google_recaptcha_token={recaptcha_token}")
        print(authReq.text)
        print(authReq.cookies)
        if authReq.status_code != 200:
            return authReq.text
        elif "Failed to create account" in authReq.text:
            return "Something is wrong with your login."
        else:
            return True

    def check_working(self, cookies):
        resp = curl_requests.post("https://www.myvipon.com/api2/passport/email-status", cookies=cookies, impersonate="chrome")
        if resp.status_code == 401:
            return False

        resp2 = curl_requests.get("https://www.myvipon.com/shopper/request/index?ref=shopper_request", cookies=cookies, impersonate="chrome")
        lines = resp2.text.splitlines()
        for line in lines:
            if "Remaining Vouchers:" in line:
                amt = lines[lines.index(line) + 1].split(" (")[0].replace("<p>", "")
                if int(amt) < 1:
                    return False

        return True

    def load_account(self, account):
        if self.check_working(account):
            print("Adding account")
            self.working.append(account)
        else:
            print("Account not working: " + str(account))

    def rotate_accounts(self):
        now = time.time()
        recycled = []
        for acct, limited_at in self.limit:
            if now - limited_at >= 86400:
                recycled.append((acct, limited_at))
        for entry in recycled:
            self.limit.remove(entry)
            self.working.append(entry[0])
            print(f"[AccountRotation] Recycled a rate-limited account (waited {int(now - entry[1])}s)")

        if not self.working:
            self.current = None
            return

        self.current = self.working.pop(0)

        if not self.check_working(self.current):
            self.limit.append((self.current, now))
            return self.rotate_accounts()

        self.session.cookies.update(self.current)

        try:
            self.session.get("https://myvipon.com/", headers=self.headers)
        except Exception:
            self.rotate_accounts()

        return True

    def _is_cloudflare_block(self, text):
        markers = ["Cloudflare to restrict access", "Just a moment", "cf-error-details", "Checking your browser"]
        return any(m in text for m in markers)

    def handle_first_request(self, idd, _retries=0):
        if _retries >= 3:
            print(f"[CodeFetch] Max retries reached for {idd}")
            return "rate_limited"

        url = f"https://www.myvipon.com/code/get-code?id={idd}&f=fd_web_detail&position=0&event_type=search&sl=c2ba4bd9970d893c625be5ffe811da00"

        try:
            kwargs = dict(cookies=self.current, impersonate="chrome")
            if self.code_fetch_proxies:
                kwargs["proxies"] = self.code_fetch_proxies
            first_check = curl_requests.get(url, **kwargs)
        except Exception as e:
            print(f"[CodeFetch] Request exception for {idd}: {e}")
            return "rate_limited"

        if first_check.status_code == 429 or self._is_cloudflare_block(first_check.text):
            print(f"[CodeFetch] Cloudflare blocked request for {idd} (HTTP {first_check.status_code})")
            return "rate_limited"

        if any(x in first_check.text for x in self.SUCCESSFUL):
            print(f"[CodeFetch] Success for {idd}")
            return self.return_codes(first_check.text)

        elif any(x in first_check.text for x in self.FAILED):
            if self.check_for_captcha(first_check.text):
                print(f"[CodeFetch] Captcha for {idd}, solving...")
                return self.handle_captcha(idd)

            elif "Invalid Request" in first_check.text:
                print(f"[CodeFetch] Invalid Request for {idd}, retrying ({_retries + 1}/3)...")
                time.sleep(3)
                return self.handle_first_request(idd, _retries=_retries + 1)

            elif "Not more than 30 vouchers within 24 hours" in first_check.text:
                print(f"[CodeFetch] Voucher limit hit, rotating account")
                self.rotate_accounts()
                if self.current is None:
                    return "rate_limited"
                return self.handle_first_request(idd, _retries=_retries + 1)

            elif "Oops, Instant vouchers have run out.." in first_check.text:
                return "out_of_vouchers"

        print(f"[CodeFetch] Unknown response for {idd} (HTTP {first_check.status_code})")
        return "failed"

    def handle_captcha(self, idd):

        headers = {
            "Accept": "image/avif,image/webp,*/*",
            "Accept-Language": "en-US,en;q=0.5",
            "DNT": "1",
            "Connection": "keep-alive",
            "Referer": f"https://www.myvipon.com/code/get-code?id={idd}&f=fd_web_detail&position=0&event_type=search&sl=c2ba4bd9970d893c625be5ffe811da00",
            "Sec-Fetch-Dest": "image",
            "Sec-Fetch-Mode": "no-cors",
            "Sec-Fetch-Site": "same-origin",
            "Pragma": "no-cache",
            "Cache-Control": "no-cache",
        }

        IMG_TO_SOLVE = self.session.get("https://www.myvipon.com/code/verify", headers=headers)
        print(headers)
        print(self.session.proxies)
        print(self.session.cookies.get_dict())
        
        open("captcha.png", "wb").write(IMG_TO_SOLVE.content)
        
        captcha, result = solve(IMG_TO_SOLVE.content)

        print("Solved captcha: " + captcha)
        print(result)

        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://www.myvipon.com",
            "DNT": "1",
            "Connection": "keep-alive",
            "Referer": f"https://www.myvipon.com/code/get-code?id={idd}&f=fd_web_detail&position=0&event_type=search&sl=c2ba4bd9970d893c625be5ffe811da00",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
            "Pragma": "no-cache",
            "Cache-Control": "no-cache",
        }

        SOLVE_REQUEST = self.session.post(
            f"https://www.myvipon.com/code/check?id={idd}&sl=c2ba4bd9970d893c625be5ffe811da00&f=fd_web_detail&search_id=0&event_type=search&position=0",
            headers=headers,
            data=f"verifycode={result}&signup-button=")

        if any(x in SOLVE_REQUEST.text for x in self.SUCCESSFUL):
            print("Success")
            return self.return_codes(SOLVE_REQUEST.text)

        elif any(x in SOLVE_REQUEST.text for x in self.FAILED):
            if self.check_for_captcha(SOLVE_REQUEST.text):
                print("Captcha detected, solving...2")

                print("Debugging...")
                print(
                    "URL: " + f"https://www.myvipon.com/code/check?id={idd}&sl=c2ba4bd9970d893c625be5ffe811da00&f=fd_web_detail&search_id=0&event_type=search&position=0")
                print("Headers: " + str(headers))
                print("Data: " + f"verifycode={result}&signup-button=")

                return self.handle_captcha(idd)

            elif "Invalid Request" in SOLVE_REQUEST.text:
                print("Invalid Request, retrying...2")
                return self.handle_first_request(idd)

            elif "Not more than 30 vouchers within 24 hours" in SOLVE_REQUEST.text:
                self.rotate_accounts()
                return self.handle_first_request(idd)

            pass

        print("Failed")
        print(SOLVE_REQUEST.text)
        return ["Something went wrong"]

    def get_code(self, idd):
        if self.current is None:
            print("No accounts loaded")
            return ["No accounts loaded"]

        if self.code_fetch_proxies:
            saved = getattr(self.session, "proxies", None)
            self.session.proxies = self.code_fetch_proxies
            try:
                return self.handle_first_request(idd)
            finally:
                self.session.proxies = saved or {}
        return self.handle_first_request(idd)

    def check_for_captcha(self, data):
        if "Please complete the equation below to continue." in data:
            return True
        return False

    def return_codes(self, data):
        parsePage = bs4.BeautifulSoup(data, "html.parser")

        try:
            codeContainer = parsePage.find("div", {"class": "code-container"}).text
        except:
            print("WARNING: Could not find code container")
            return "This shouldn't of happened!"

        print(codeContainer)

        codeContainer = codeContainer.replace("You have requested this code previously. CODE: ", "")
        codeContainer = codeContainer.replace("CODE: ", "")

        return codeContainer

    def get_amz_link(self, idd):
        req = self.session.get(f"https://www.myvipon.com/product/open-amazon?id={idd}&event_type=search&position=0",
                               headers=self.headers)

        if req.status_code == 302:
            # print(req.headers)
            # print(req.text)
            return req.headers["X-Redirect"]
        else:
            print(req.text)
            print(req.history)
            return "Something went wrong, please report this"

    def set_fufillment(self, fulfillment=None):  # 1 = merchant, 0 = amazon
        if fulfillment == "merchant":
            self.fulfillment = "1"
            return True
        elif fulfillment == "amazon":
            self.fulfillment = "0"
            return True
        elif fulfillment == None:
            self.fulfillment = ""
            return True

        return False

    def validate_discount(self, discount=None):  # all, 20-49, 50-79, 80-101
        if discount == "all":
            self.discount = ""
            return True
        elif discount == "20-49":
            self.discount = "20-49"
            return True
        elif discount == "50-79":
            self.discount = "50-79"
            return True
        elif discount == "80-101":
            self.discount = "80-101"
            return True
        elif discount == None:
            self.discount = ""
            return True

        return False

    def validate_status(self, status=None):  # instant, upcoming
        if status == "instant":
            self.status = "instant"
            return True
        elif status == "upcoming":
            self.status = "upcoming"
            return True
        elif status == None:
            self.status = ""
            return True

        return False

    def validate_category(self, category=None):
        if category in self.categories:
            self.category += self.categories[category] + "*"
            return True
        elif category == None:
            self.category = ""
            return True

        return False

    def validate_sort(self, sort=None):
        if sort in self.sort:
            return self.sort[sort]
        else:
            return ""

    def set_price(self, beg=None, end=None):
        if beg == None or end == None:
            return ""
        return f"{beg}-{end}"

    def set_type(self, typeGiven=None):
        if typeGiven == "deals":
            self.status = "deal"
        elif typeGiven == "coupons":
            self.status = "coupons"
        elif typeGiven == None:
            self.status = ""
        return True

    def set_page(self, page):
        self.page = page
        return True

    def reset(self):
        self.fulfillment = ""
        self.discount = ""
        self.status = ""
        self.priceRange = ""
        self.category = ""
        self.type = ""
        self.page = ""
        return True

    def validate_resp(self, output, func):
        if output:
            return True
        print("Error on routine: " + func.__name__)
        return False

    def get_coupons(self, fufillment, discount, category, sorting, price, page):

        # Check if category ends with *, if so remove
        if self.category.endswith("*"):
            self.category = self.category[:-1]

        # if self.debug:
        print(self.base + f"promotion/search/?search=&domain={self.domain}&group={category}&category_id={category}&uid=0&sort={sorting}&type=instant&fba={fufillment}&price={price}&discount={discount}&page={page}")

        req = self.session.get(
            self.base + f"promotion/search/?search=&domain={self.domain}&group={category}&category_id={category}&uid=0&sort={sorting}&type=instant&fba={fufillment}&price={price}&discount={discount}&page={page}")

        if self.debug:
            # print(req.text)
            print(req.status_code)

        if req.status_code != 200:
            print(f"[API] get_coupons failed: HTTP {req.status_code}")
            return {"status": "error", "message": req.text, "code": str(req.status_code)}
        try:
            return {"status": "success", "data": req.json()["html"]}
        except (KeyError, ValueError) as e:
            print(f"[API] get_coupons unexpected response: {e} — {req.text[:500]}")
            return {"status": "error", "message": str(e), "code": str(req.status_code)}

    def get_coupons_search(self, search, fufillment, discount, category, sorting, price, page):

        url = "https://search.myvipon.com/es/viponpc/search"

        headers = {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "en-US,en;q=0.5",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://www.myvipon.com",
            "DNT": "1",
            "Connection": "keep-alive",
            "Referer": "https://www.myvipon.com/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
            "Pragma": "no-cache",
            "Cache-Control": "no-cache",
        }

        data = f"search={search}&group={category}&page={page}&discount={discount}&domain=www.amazon.com&fba={fufillment}&sort={sorting}&price={price}"

        # if self.debug:
        print(data)

        req = self.session.post(url, headers=headers, data=data)
        
        if req.status_code != 200:
            print(f"[API] get_coupons_search failed: HTTP {req.status_code} — {req.text[:500]}")
            return {"status": "error", "message": req.text, "code": str(req.status_code)}
        try:
            return {"status": "success", "data": req.json()["data"]}
        except (KeyError, ValueError) as e:
            print(f"[API] get_coupons_search unexpected response: {e} — {req.text[:500]}")
            return {"status": "error", "message": str(e), "code": str(req.status_code)}

    def parse(self, data):
        products = {}
        scraper = bs4.BeautifulSoup(data, "html.parser")
        counter = 0
        for div in scraper.find_all("div", {"class": "box solid"}):
            product = {}

            if self.debug:
                print(div)

            img_src = div.find("div", {"class": "box-img"}).find("img").get("src")
            coupon_src = div.get("id").split("-")[1]
            info = div.find("div", {"class": "content-text"})
            title = info.find_all("span")[0].text
            fulfillment = info.find_all("span")[1].text
            regular_price = info.find("s", {"class": "price"}).text
            discount = info.find("div", {"class": "discound"}).text
            discounted_price = info.find("span", {"class": "discound-price"}).text
            vipon_url = "https://myvipon.com/product/" + div.get("data-id")
            daId = div.get("data-id").replace("id", "")

            # getCoupons = self.get_code(daId)
            # time.sleep(random.randint(1,3))

            product["img_src"] = img_src
            product["coupon_src"] = coupon_src
            product["title"] = title
            # Truncate title if longer than 230 characters
            if len(product["title"]) > 230:
                product["title"] = product["title"][0:230] + "..."

            product["fulfillment"] = fulfillment.strip()
            product["regular_price"] = regular_price
            
            # Remove "-" from discount
            discount = discount.replace("-", "")
            
            product["discount"] = discount
            product["discounted_price"] = discounted_price
            product["url"] = vipon_url
            product["id"] = daId
            product["amz_link"] = self.get_amz_link(daId)

            # product["coupon_codes"] = getCoupons

            products[counter] = product
            counter += 1

        return products

    def parse_search(self, data):
        products = {}
        counter = 0

        for product in data:
            # print(product)
            if not product.isnumeric():
                continue
            try:
                product = data[product]
            except TypeError:
                print("whjythishappen")
                print(product)
                print(data)

            # print(product)

            temp = {}

            temp["img_src"] = product["image_large"]
            temp["title"] = product["art_name"]

            # Truncate title if longer than 230 characters
            if len(temp["title"]) > 230:
                temp["title"] = temp["title"][0:230] + "..."

            if product["fba"] == "FBA":
                temp["fulfillment"] = "Amazon"
            else:
                temp["fulfillment"] = "Merchant"
            temp["regular_price"] = product["price_format"]
            temp["discount"] = product["discount_display"]
            temp["discounted_price"] = product["final_price_format"]
            temp["id"] = product["product_id"]
            temp["amz_link"] = "https://www.amazon.com/gp/product/" + product["parent_asin"]
            if product["shipping"] == 0:
                temp["shipping"] = "Free Shipping"
            else:
                temp["shipping"] = f"${product['shipping']} Shipping"
            temp["review"] = str(product["review_star"])
            temp["review_count"] = str(product["review_num"])
            temp["category"] = product["category"]

            products[counter] = temp

            counter += 1

        return products

    def process(self, data):
        return data.encode("utf-8").decode("unicode-escape")


def align_characters(image, bg_height):
    # Calculate the vertical position to center the processed image
    y_position = (bg_height - image.shape[0]) // 2

    # Create a black background
    black_bg = np.zeros((bg_height, image.shape[1]), dtype=np.uint8)

    # Paste the processed image onto the black background at the centered position
    black_bg[y_position:y_position + image.shape[0], 0:image.shape[1]] = image

    return black_bg


def solve(img):
    # Use image bytes
    img = cv2.imdecode(np.frombuffer(img, np.uint8), cv2.IMREAD_COLOR)

    psmToUse = "10"
    # Cvt to hsv
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

    # Cut image to only show the captcha not the question mark at the end
    # It's a little bit after halfway through the image
    hsv = hsv[0:hsv.shape[0], 0:int(hsv.shape[1] / 2)]

    # Get binary-mask
    msk = cv2.inRange(hsv, np.array([0, 0, 200]), np.array([179, 255, 255]))
    krn = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 3))
    dlt = cv2.dilate(msk, krn, iterations=1)
    thr = 255 - cv2.bitwise_and(dlt, msk)

    # # show the image
    # cv2.imshow("thr", thr)
    # cv2.waitKey(0)

    # Split image into 3 parts, 1st digit, operator, 2nd digit
    # 1st digit
    firstDigit = thr[0:thr.shape[0], 0:int(thr.shape[1] / 3)]
    # Operator
    operator = thr[0:thr.shape[0], int(thr.shape[1] / 3):int(thr.shape[1] / 3 * 2)]
    # 2nd digit
    secondDigit = thr[0:thr.shape[0], int(thr.shape[1] / 3 * 2):thr.shape[1]]

    # Calculate the height of the black background
    bg_height = img.shape[0]

    # Align and OCR the first digit
    aligned_first_digit = align_characters(firstDigit, bg_height)
    st = pytesseract.image_to_string(aligned_first_digit, lang="eng", config="--psm " + psmToUse).strip().replace("\n",
                                                                                                                  "")
    try:
        int(st)
    except:
        psmToUse = "8"
        st = pytesseract.image_to_string(aligned_first_digit, lang="eng", config="--psm " + psmToUse).strip().replace(
            "?", "").replace("\n", "")
        psmToUse = "10"
    # print("First Digit: " + st)

    # Align and OCR the operator
    aligned_operator = align_characters(operator, bg_height)
    nd = pytesseract.image_to_string(aligned_operator, lang="eng", config="--psm " + psmToUse).replace("\n", "")

    # print("Operator: " + nd)

    # Align and OCR the second digit
    aligned_second_digit = align_characters(secondDigit, bg_height)
    # cv2.imshow("aligned_second_digit", aligned_second_digit)
    rd = pytesseract.image_to_string(aligned_second_digit, lang="eng", config="--psm " + psmToUse).replace("\n", "")
    try:
        int(rd)
    except:
        psmToUse = "8"
        rd = pytesseract.image_to_string(aligned_second_digit, lang="eng", config="--psm " + psmToUse).strip().replace(
            "?", "").replace("\n", "")
        psmToUse = "10"
    # print("Second Digit: " + rd)

    result = ""

    if "=" in nd:
        nd = nd.replace("=", "-")

    if "+" in nd:
        result = int(st) + int(rd)
    elif "-" in nd:
        result = int(st) - int(rd)

    captcha = st + nd + rd + "=" + str(result)

    result = str(result).strip()

    return captcha, result
