import base64
import json
import os
import time
import urllib

import boto3
import jwt
import rsa
from botocore.exceptions import ClientError

jwks_uri = os.getenv("JWKS_URI", "TODO:REPLACEME")
key_pair_id = os.getenv("SIGNING_KEY_ID", "TODO:REPLACEME")
oidc_client_id = os.getenv("OIDC_CLIENT_ID", "TODO:REPLACEME")
region = os.getenv("AWS_REGION", os.getenv("AWS_DEFAULT_REGION", "us-east-1"))
secret_path = os.getenv("SIGNING_KEY_SECRET_PATH", "TODO:REPLACEME")
signature_expiration_days = int(os.getenv("SIGNATURE_EXPIRATION_DAYS", 1))
link_bucket = os.getenv("LINK_BUCKET", "dengo-links")


def load_cf_signing_key():
    session = boto3.session.Session()
    sm_client = session.client(service_name="secretsmanager", region_name=region)
    try:
        get_secret_value_response = sm_client.get_secret_value(SecretId=secret_path)
    except ClientError as e:
        raise e
    signing_key_pem = get_secret_value_response["SecretString"]
    return rsa.PrivateKey.load_pkcs1(signing_key_pem.encode("utf8"))


def load_jwks_keys():
    jwks_client = jwt.PyJWKClient(jwks_uri)
    _ = jwks_client.fetch_data()
    return jwks_client


### exec at init
cloudfront_signing_translation = str.maketrans("+=/", "-_~")
signing_key = load_cf_signing_key()
jwks_client = load_jwks_keys()
### exec at init


def cloudfront_urlsafe_b64(thing):
    b64_string = base64.b64encode(thing).decode()
    return b64_string.translate(cloudfront_signing_translation)


def gen_signature():
    headers = {}
    expiration_time = int(time.time()) + (86400 * signature_expiration_days)
    shared_policy = {"Statement": [{"Condition": {"DateLessThan": {"AWS:EpochTime": expiration_time}}}]}
    # `separators` will remove whitespace within encoded json
    shared_policy_json_bytes = json.dumps(shared_policy, separators=(",", ":")).encode("utf8")
    signature = rsa.sign(shared_policy_json_bytes, signing_key, "SHA-1")
    headers["CloudFront-Policy"] = cloudfront_urlsafe_b64(shared_policy_json_bytes)
    headers["CloudFront-Signature"] = cloudfront_urlsafe_b64(signature)
    headers["CloudFront-Key-Pair-Id"] = key_pair_id
    return headers


def kv_to_cookies(headers):
    cookies = []
    for k, v in headers.items():
        cookies.append(f"{k}={v}; Secure; HttpOnly; Path=/")
    return cookies


def encode_identity_cookie(identity):
    signature = rsa.sign(identity.encode(), signing_key, "SHA-1")
    signature = base64.b64encode(signature).decode()
    sig_data = json.dumps({"identity": identity, "signature": signature}).encode("utf8")
    return {"Dengo-Identity": cloudfront_urlsafe_b64(sig_data)}


def decode_identity_cookie(cookie):
    if cookie is None:
        return None

    sig_data = json.loads(base64.b64decode(cookie))
    identity = sig_data["identity"].encode()
    signature = base64.b64decode(sig_data["signature"])
    try:
        if rsa.verify(identity, signature, signing_key) == "SHA-1":
            return identity.decode()
    except rsa.pkcs1.VerificationError:
        pass
    return None


def set_redirect(request):
    params = urllib.parse.parse_qs(request.get("rawQueryString", ""))
    return_target = params.get("target_path", [""])[0]
    if return_target.startswith("/"):
        return_target = return_target[1:]
    return_target_safe = urllib.parse.quote(return_target)
    return f"/{return_target_safe}"


def check_oidc_auth(jwt_data):
    try:
        signing_key = jwks_client.get_signing_key_from_jwt(jwt_data)
        data = jwt.decode(jwt_data, signing_key.key, algorithms=["RS256"], audience=oidc_client_id)
        if data.get("email_verified", False):
            return data["email"]
    except jwt.ExpiredSignatureError:
        pass
    except jwt.InvalidTokenError:
        pass

    return None


def find_cookie(event, find_name):
    cookies = event.get("cookies", [])
    for cookie in cookies:
        cookie_name, value = cookie.split("=", 1)
        if cookie_name == find_name:
            return value
    return None


def event_post_data(event, unique=True):
    return_data = {}
    if event.get("requestContext", {}).get("http", {}).get("method", "") == "POST":
        post_data = event.get("body", "")
        if event.get("isBase64Encoded", False):
            post_data = base64.b64decode(post_data).decode("utf8")
        post_data = urllib.parse.parse_qs(post_data)
        if not unique:
            return post_data

        for k, v in post_data.items():
            return_data[k] = v[0]

    return return_data


def link_handler(event, context):
    response = {"statusCode": 403, "body": "Invalid link or you are not owner"}
    update_or_create = "failed"
    identity_cookie = find_cookie(event, "Dengo-Identity")
    identity = decode_identity_cookie(identity_cookie)
    if identity is not None:
        post_data = event_post_data(event)
        link_name = post_data.get("name", "")
        session = boto3.session.Session()
        s3_client = session.client(service_name="s3", region_name=region)

        ownership_verified = False

        try:
            s3_client.head_object(Bucket=link_bucket, Key=link_name)
        except ClientError as e:
            ownership_verified = True
            update_or_create = "created"

        if not ownership_verified:
            tags = s3_client.get_object_tagging(Bucket=link_bucket, Key=link_name).get("TagSet", [])

            for tag in tags:
                if tag["Key"] == "DengoOwner" and tag["Value"] == identity:
                    update_or_create = "updated"
                    ownership_verified = True
                    break

        # object doesn't exist, or user is the owner
        # we can take ownership and publish the link
        if ownership_verified:
            link_url = post_data.get("url", "")
            link_body = f'Redirecting to <a href="{link_url}">{link_url}</a>'
            s3_client.put_object(
                Body=link_body,
                Bucket=link_bucket,
                Key=link_name,
                WebsiteRedirectLocation=link_url,
                Tagging="DengoOwner=" + identity,
            )
            body_json = json.dumps(
                {
                    "url": link_url,
                    "short": link_name,
                    "owner": identity,
                    "status": update_or_create,
                    "message": f"Link {link_name} {update_or_create} for {link_url}",
                }
            )
            response = {"statusCode": 200, "body": body_json}
    return response


def auth_handler(event, context):
    response = {"statusCode": 401, "body": "Unauthorized"}
    if event.get("requestContext", {}).get("http", {}).get("method", "") == "POST":
        post_data = event_post_data(event)
        identity = check_oidc_auth(post_data.get("id_token", ""))
        if identity is not None:
            cookies = gen_signature()
            cookies.update(encode_identity_cookie(identity))
            response = {
                "statusCode": 302,
                "headers": {
                    "Location": set_redirect(event),
                },
                "body": "",
                "cookies": kv_to_cookies(cookies),
                "isBase64Encoded": False,
            }

    return response
