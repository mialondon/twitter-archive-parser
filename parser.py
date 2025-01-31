#!/usr/bin/env python3
"""
    twitter-archive-parser - Python code to parse a Twitter archive and output in various ways
    Copyright (C) 2022 Tim Hutton - https://github.com/timhutton/twitter-archive-parser

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""

from collections import defaultdict
from urllib.parse import urlparse
import datetime
import glob
import importlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
# hot-loaded if needed, see import_module():
#  imagesize
#  requests


# Print a compile-time error in Python < 3.6. This line does nothing in Python 3.6+ but is reported to the user
# as an error (because it is the first line that fails to compile) in older versions.
f' Error: This script requires Python 3.6 or later. Use `python --version` to check your version.'


class UserData:
    def __init__(self, id, handle = None):
        self.id = id
        self.handle = handle


def import_module(module):
    """Imports a module specified by a string. Example: requests = import_module('requests')"""
    try:
        return importlib.import_module(module)
    except ImportError:
        print(f'\nError: This script uses the "{module}" module which is not installed.\n')
        user_input = input('OK to install using pip? [y/n]')
        if not user_input.lower() in ('y', 'yes'):
            exit()
        subprocess.run([sys.executable, '-m', 'pip', 'install', module], check=True)
        return importlib.import_module(module)


def get_twitter_api_guest_token(session, bearer_token):
    """Returns a Twitter API guest token for the current session."""
    guest_token_response = session.post("https://api.twitter.com/1.1/guest/activate.json",
                                        headers={'authorization': f'Bearer {bearer_token}'},
                                        timeout=2,
                                        )
    guest_token = json.loads(guest_token_response.content)['guest_token']
    if not guest_token:
        raise Exception(f"Failed to retrieve guest token")
    return guest_token


def get_twitter_users(session, bearer_token, guest_token, user_ids):
    """Asks Twitter for all metadata associated with user_ids."""
    users = {}
    while user_ids:
        max_batch = 100
        user_id_batch = user_ids[:max_batch]
        user_ids = user_ids[max_batch:]
        user_id_list = ",".join(user_id_batch)
        query_url = f"https://api.twitter.com/1.1/users/lookup.json?user_id={user_id_list}"
        response = session.get(query_url,
                               headers={'authorization': f'Bearer {bearer_token}', 'x-guest-token': guest_token},
                               timeout=2,
                               )
        if not response.status_code == 200:
            raise Exception(f'Failed to get user handle: {response}')
        response_json = json.loads(response.content)
        for user in response_json:
            users[user["id_str"]] = user
    return users


def lookup_users(user_ids, users):
    """Fill the users dictionary with data from Twitter"""
    # Filter out any users already known
    filtered_user_ids = [id for id in user_ids if id not in users]
    if not filtered_user_ids:
        # Don't bother opening a session if there's nothing to get
        return
    # Account metadata observed at ~2.1KB on average.
    estimated_size = int(2.1 * len(filtered_user_ids))
    print(f'{len(filtered_user_ids)} users are unknown.')
    user_input = input(f'Download user data from Twitter (approx {estimated_size:,}KB)? [y/n]')
    if user_input.lower() not in ('y', 'yes'):
        return
    requests = import_module('requests')
    try:
        with requests.Session() as session:
            bearer_token = 'AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA'
            guest_token = get_twitter_api_guest_token(session, bearer_token)
            retrieved_users = get_twitter_users(session, bearer_token, guest_token, filtered_user_ids)
            for user_id, user in retrieved_users.items():
                users[user_id] = UserData(user_id, user["screen_name"])
    except Exception as err:
        print(f'Failed to download user data: {err}')

def read_json_from_js_file(filename):
    """Reads the contents of a Twitter-produced .js file into a dictionary."""
    print(f'Parsing {filename}...')
    with open(filename, 'r', encoding='utf8') as f:
        data = f.readlines()
        # if the JSON has no real content, it can happen that the file is only one line long.
        # in this case, return an empty dict to avoid errors while trying to read non-existing lines.
        if len(data) <= 1:
            return {}
        # convert js file to JSON: replace first line with just '[', squash lines into a single string
        prefix = '['
        if '{' in data[0]:
            prefix += ' {'
        data =  prefix + ''.join(data[1:])
        # parse the resulting JSON and return as a dict
        return json.loads(data)


def extract_username(paths):
    """Returns the user's Twitter username from account.js."""
    account = read_json_from_js_file(paths.file_account_js)
    return account[0]['account']['username']


def convert_tweet(tweet, username, media_sources, users, paths):
    """Converts a JSON-format tweet. Returns tuple of timestamp, markdown and HTML."""
    if 'tweet' in tweet.keys():
        tweet = tweet['tweet']
    timestamp_str = tweet['created_at']
    timestamp = int(round(datetime.datetime.strptime(timestamp_str, '%a %b %d %X %z %Y').timestamp())) # Example: Tue Mar 19 14:05:17 +0000 2019
    body_markdown = tweet['full_text']
    body_html = tweet['full_text']
    tweet_id_str = tweet['id_str']
    # for old tweets before embedded t.co redirects were added, ensure the links are
    # added to the urls entities list so that we can build correct links later on.
    if 'entities' in tweet and 'media' not in tweet['entities'] and len(tweet['entities'].get("urls", [])) == 0:
        for word in tweet['full_text'].split():
            url = urlparse(word)
            if url.scheme != '' and url.netloc != '' and not word.endswith('\u2026'):
                # Shorten links similiar to twitter
                netloc_short = url.netloc[4:] if url.netloc.startswith("www.") else url.netloc
                path_short = url.path if len(url.path + '?' + url.query) < 15 else (url.path + '?' + url.query)[:15] + '\u2026'
                tweet['entities']['urls'].append({
                    'url': word,
                    'expanded_url': word,
                    'display_url': netloc_short + path_short,
                    'indices': [tweet['full_text'].index(word), tweet['full_text'].index(word) + len(word)],
                })
    # replace t.co URLs with their original versions
    if 'entities' in tweet and 'urls' in tweet['entities']:
        for url in tweet['entities']['urls']:
            if 'url' in url and 'expanded_url' in url:
                expanded_url = url['expanded_url']
                body_markdown = body_markdown.replace(url['url'], expanded_url)
                expanded_url_html = f'<a href="{expanded_url}">{expanded_url}</a>'
                body_html = body_html.replace(url['url'], expanded_url_html)
    # if the tweet is a reply, construct a header that links the names of the accounts being replied to the tweet being replied to
    header_markdown = ''
    header_html = ''
    if 'in_reply_to_status_id' in tweet:
        # match and remove all occurences of '@username ' at the start of the body
        replying_to = re.match(r'^(@[0-9A-Za-z_]* )*', body_markdown)[0]
        if replying_to:
            body_markdown = body_markdown[len(replying_to):]
            body_html = body_html[len(replying_to):]
        else:
            # no '@username ' in the body: we're replying to self
            replying_to = f'@{username}'
        names = replying_to.split()
        # some old tweets lack 'in_reply_to_screen_name': use it if present, otherwise fall back to names[0]
        in_reply_to_screen_name = tweet['in_reply_to_screen_name'] if 'in_reply_to_screen_name' in tweet else names[0]
        # create a list of names of the form '@name1, @name2 and @name3' - or just '@name1' if there is only one name
        name_list = ', '.join(names[:-1]) + (f' and {names[-1]}' if len(names) > 1 else names[0])
        in_reply_to_status_id = tweet['in_reply_to_status_id']
        replying_to_url = f'https://twitter.com/{in_reply_to_screen_name}/status/{in_reply_to_status_id}'
        header_markdown += f'Replying to [{name_list}]({replying_to_url})\n\n'
        header_html += f'Replying to <a href="{replying_to_url}">{name_list}</a><br>'
    # replace image URLs with image links to local files
    if 'entities' in tweet and 'media' in tweet['entities'] and 'extended_entities' in tweet and 'media' in tweet['extended_entities']:
        original_url = tweet['entities']['media'][0]['url']
        markdown = ''
        html = ''
        for media in tweet['extended_entities']['media']:
            if 'url' in media and 'media_url' in media:
                original_expanded_url = media['media_url']
                original_filename = os.path.split(original_expanded_url)[1]
                archive_media_filename = tweet_id_str + '-' + original_filename
                archive_media_path = os.path.join(paths.dir_input_media, archive_media_filename)
                file_output_media = os.path.join(paths.dir_output_media, archive_media_filename)
                media_url = f'{os.path.split(paths.dir_output_media)[1]}/{archive_media_filename}'
                markdown += '' if not markdown and body_markdown == original_url else '\n\n'
                html += '' if not html and body_html == original_url else '<br>'
                if os.path.isfile(archive_media_path):
                    # Found a matching image, use this one
                    if not os.path.isfile(file_output_media):
                        shutil.copy(archive_media_path, file_output_media)
                    markdown += f'![]({media_url})'
                    html += f'<img src="{media_url}"/>'
                    # Save the online location of the best-quality version of this file, for later upgrading if wanted
                    best_quality_url = f'https://pbs.twimg.com/media/{original_filename}:orig'
                    media_sources.append((os.path.join(paths.dir_output_media, archive_media_filename), best_quality_url))
                else:
                    # Is there any other file that includes the tweet_id in its filename?
                    archive_media_paths = glob.glob(os.path.join(paths.dir_input_media, tweet_id_str + '*'))
                    if len(archive_media_paths) > 0:
                        for archive_media_path in archive_media_paths:
                            archive_media_filename = os.path.split(archive_media_path)[-1]
                            file_output_media = os.path.join(paths.dir_output_media, archive_media_filename)
                            media_url = f'{os.path.split(paths.dir_output_media)[1]}/{archive_media_filename}'
                            if not os.path.isfile(file_output_media):
                                shutil.copy(archive_media_path, file_output_media)
                            markdown += f'<video controls><source src="{media_url}">Your browser does not support the video tag.</video>\n'
                            html += f'<video controls><source src="{media_url}">Your browser does not support the video tag.</video>\n'
                            # Save the online location of the best-quality version of this file, for later upgrading if wanted
                            if 'video_info' in media and 'variants' in media['video_info']:
                                best_quality_url = ''
                                best_bitrate = -1 # some valid videos are marked with bitrate=0 in the JSON
                                for variant in media['video_info']['variants']:
                                    if 'bitrate' in variant:
                                        bitrate = int(variant['bitrate'])
                                        if bitrate > best_bitrate:
                                            best_quality_url = variant['url']
                                            best_bitrate = bitrate
                                if best_bitrate == -1:
                                    print(f"Warning No URL found for {original_url} {original_expanded_url} {archive_media_path} {media_url}")
                                    print(f"JSON: {tweet}")
                                else:
                                    media_sources.append((os.path.join(paths.dir_output_media, archive_media_filename), best_quality_url))
                    else:
                        print(f'Warning: missing local file: {archive_media_path}. Using original link instead: {original_url} (expands to {original_expanded_url})')
                        markdown += f'![]({original_url})'
                        html += f'<a href="{original_url}">{original_url}</a>'
        body_markdown = body_markdown.replace(original_url, markdown)
        body_html = body_html.replace(original_url, html)
    # make the body a quote
    body_markdown = '> ' + '\n> '.join(body_markdown.splitlines())
    body_html = '<p><blockquote>' + '<br>\n'.join(body_html.splitlines()) + '</blockquote>'
    # append the original Twitter URL as a link
    original_tweet_url = f'https://twitter.com/{username}/status/{tweet_id_str}'
    body_markdown = header_markdown + body_markdown + f'\n\n<img src="{paths.file_tweet_icon}" width="12" /> [{timestamp_str}]({original_tweet_url})'
    body_html = header_html + body_html + f'<a href="{original_tweet_url}"><img src="{paths.file_tweet_icon}" width="12" />&nbsp;{timestamp_str}</a></p>'
    # extract user_id:handle connections
    if 'in_reply_to_user_id' in tweet and 'in_reply_to_screen_name' in tweet:
        id = tweet['in_reply_to_user_id']
        if int(id) >= 0: # some ids are -1, not sure why
            handle = tweet['in_reply_to_screen_name']
            users[id] = UserData(id=id, handle=handle)
    if 'entities' in tweet and 'user_mentions' in tweet['entities']:
        for mention in tweet['entities']['user_mentions']:
            id = mention['id']
            if int(id) >= 0: # some ids are -1, not sure why
                handle = mention['screen_name']
                users[id] = UserData(id=id, handle=handle)

    return timestamp, body_markdown, body_html


def find_files_input_tweets(dir_path_input_data):
    """Identify the tweet archive's file and folder names - they change slightly depending on the archive size it seems."""
    input_tweets_file_templates = ['tweet.js', 'tweets.js', 'tweets-part*.js']
    files_paths_input_tweets = []
    for input_tweets_file_template in input_tweets_file_templates:
        files_paths_input_tweets += glob.glob(os.path.join(dir_path_input_data, input_tweets_file_template))
    if len(files_paths_input_tweets)==0:
        print(f'Error: no files matching {input_tweets_file_templates} in {dir_path_input_data}')
        exit()
    return files_paths_input_tweets


def find_dir_input_media(dir_path_input_data):
    input_media_dir_templates = ['tweet_media', 'tweets_media']
    input_media_dirs = []
    for input_media_dir_template in input_media_dir_templates:
        input_media_dirs += glob.glob(os.path.join(dir_path_input_data, input_media_dir_template))
    if len(input_media_dirs) == 0:
        print(f'Error: no folders matching {input_media_dir_templates} in {dir_path_input_data}')
        exit()
    if len(input_media_dirs) > 1:
        print(f'Error: multiple folders matching {input_media_dir_templates} in {dir_path_input_data}')
        exit()
    return input_media_dirs[0]


def download_file_if_larger(url, filename, index, count, sleep_time):
    """Attempts to download from the specified URL. Overwrites file if larger.
       Returns whether the file is now known to be the largest available, and the number of bytes downloaded.
    """
    requests = import_module('requests')
    imagesize = import_module('imagesize')

    pref = f'{index:3d}/{count:3d} {filename}: '
    # Sleep briefly, in an attempt to minimize the possibility of trigging some auto-cutoff mechanism
    if index > 1:
        print(f'{pref}Sleeping...', end='\r')
        time.sleep(sleep_time)
    # Request the URL (in stream mode so that we can conditionally abort depending on the headers)
    print(f'{pref}Requesting headers for {url}...', end='\r')
    byte_size_before = os.path.getsize(filename)
    try:
        with requests.get(url, stream=True, timeout=2) as res:
            if not res.status_code == 200:
                # Try to get content of response as `res.text`. For twitter.com, this will be empty in most (all?) cases.
                # It is successfully tested with error responses from other domains.
                raise Exception(f'Download failed with status "{res.status_code} {res.reason}". Response content: "{res.text}"')
            byte_size_after = int(res.headers['content-length'])
            if (byte_size_after != byte_size_before):
                # Proceed with the full download
                tmp_filename = filename+'.tmp'
                print(f'{pref}Downloading {url}...            ', end='\r')
                with open(tmp_filename,'wb') as f:
                    shutil.copyfileobj(res.raw, f)
                post = f'{byte_size_after/2**20:.1f}MB downloaded'
                width_before, height_before = imagesize.get(filename)
                width_after, height_after = imagesize.get(tmp_filename)
                pixels_before, pixels_after = width_before * height_before, width_after * height_after
                pixels_percentage_increase = 100.0 * (pixels_after - pixels_before) / pixels_before

                if (width_before == -1 and height_before == -1 and width_after == -1 and height_after == -1):
                    # could not check size of both versions, probably a video or unsupported image format
                    os.replace(tmp_filename, filename)
                    bytes_percentage_increase = 100.0 * (byte_size_after - byte_size_before) / byte_size_before
                    logging.info(f'{pref}SUCCESS. New version is {bytes_percentage_increase:3.0f}% '
                                 f'larger in bytes (pixel comparison not possible). {post}')
                    return True, byte_size_after
                elif (width_before == -1 or height_before == -1 or width_after == -1 or height_after == -1):
                    # could not check size of one version, this should not happen (corrupted download?)
                    logging.info(f'{pref}SKIPPED. Pixel size comparison inconclusive: '
                                 f'{width_before}*{height_before}px vs. {width_after}*{height_after}px. {post}')
                    return False, byte_size_after
                elif (pixels_after >= pixels_before):
                    os.replace(tmp_filename, filename)
                    bytes_percentage_increase = 100.0 * (byte_size_after - byte_size_before) / byte_size_before
                    if (bytes_percentage_increase >= 0):
                        logging.info(f'{pref}SUCCESS. New version is {bytes_percentage_increase:3.0f}% larger in bytes '
                                    f'and {pixels_percentage_increase:3.0f}% larger in pixels. {post}')
                    else:
                        logging.info(f'{pref}SUCCESS. New version is actually {-bytes_percentage_increase:3.0f}% smaller in bytes '
                                f'but {pixels_percentage_increase:3.0f}% larger in pixels. {post}')
                    return True, byte_size_after
                else:
                    logging.info(f'{pref}SKIPPED. Online version has {-pixels_percentage_increase:3.0f}% smaller pixel size. {post}')
                    return True, byte_size_after
            else:
                logging.info(f'{pref}SKIPPED. Online version is same byte size, assuming same content. Not downloaded.')
                return True, 0
    except Exception as err:
        logging.error(f"{pref}FAIL. Media couldn't be retrieved from {url} because of exception: {err}")
        return False, 0


def download_larger_media(media_sources, paths):
    """Uses (filename, URL) tuples in media_sources to download files from remote storage.
       Aborts downloads if the remote file is the same size or smaller than the existing local version.
       Retries the failed downloads several times, with increasing pauses between each to avoid being blocked.
    """
    # Log to file as well as the console
    logging.basicConfig(stream=sys.stdout, level=logging.INFO, format='%(message)s')
    logfile_handler = logging.FileHandler(filename=paths.file_download_log, mode='w')
    logfile_handler.setLevel(logging.INFO)
    logging.getLogger().addHandler(logfile_handler)
    # Download new versions
    start_time = time.time()
    total_bytes_downloaded = 0
    sleep_time = 0.25
    remaining_tries = 5
    while remaining_tries > 0:
        number_of_files = len(media_sources)
        success_count = 0
        retries = []
        for index, (local_media_path, media_url) in enumerate(media_sources):
            success, bytes_downloaded = download_file_if_larger(media_url, local_media_path, index + 1, number_of_files, sleep_time)
            if success:
                success_count += 1
            else:
                retries.append((local_media_path, media_url))
            total_bytes_downloaded += bytes_downloaded
        media_sources = retries
        remaining_tries -= 1
        sleep_time += 2
        logging.info(f'\n{success_count} of {number_of_files} tested media files are known to be the best-quality available.\n')
        if len(retries) == 0:
            break
        if remaining_tries > 0:
            print(f'----------------------\n\nRetrying the ones that failed, with a longer sleep. {remaining_tries} tries remaining.\n')
    end_time = time.time()

    logging.info(f'Total downloaded: {total_bytes_downloaded/2**20:.1f}MB = {total_bytes_downloaded/2**30:.2f}GB')
    logging.info(f'Time taken: {end_time-start_time:.0f}s')
    print(f'Wrote log to {paths.file_download_log}')


def parse_tweets(username, users, html_template, paths):
    """Read tweets from paths.files_input_tweets, write to *.md and *.html.
       Copy the media used to paths.dir_output_media.
       Collect user_id:user_handle mappings for later use, in 'users'.
       Returns the mapping from media filename to best-quality URL.
   """
    tweets = []
    media_sources = []
    for tweets_js_filename in paths.files_input_tweets:
        json = read_json_from_js_file(tweets_js_filename)
        for tweet in json:
            tweets.append(convert_tweet(tweet, username, media_sources, users, paths))
    tweets.sort(key=lambda tup: tup[0]) # oldest first

    # Group tweets by month
    grouped_tweets = defaultdict(list)
    for timestamp, md, html in tweets:
        # Use a (markdown) filename that can be imported into Jekyll: YYYY-MM-DD-your-title-here.md
        dt = datetime.datetime.fromtimestamp(timestamp)
        filename = f'{dt.year}-{dt.month:02}-01-Tweet-Archive-{dt.year}-{dt.month:02}' # change to group by day or year or timestamp
        grouped_tweets[filename].append((md, html))

    for filename, content in grouped_tweets.items():
        # Write into *.md files
        md_string =  '\n\n----\n\n'.join(md for md, _ in content)
        with open(f'{filename}.md', 'w', encoding='utf-8') as f:
            f.write(md_string)

        # Write into *.html files
        html_string = '<hr>\n'.join(html for _, html in content)
        with open(f'{filename}.html', 'w', encoding='utf-8') as f:
            f.write(html_template.format(html_string))

    print(f'Wrote {len(tweets)} tweets to *.md and *.html, with images and video embedded from {paths.dir_output_media}')

    return media_sources


def parse_followings(users, URL_template_user_id, paths):
    """Parse paths.dir_input_data/following.js, write to paths.file_output_following.
       Query Twitter API for the missing user handles, if the user agrees.
    """
    following = []
    following_json = read_json_from_js_file(os.path.join(paths.dir_input_data, 'following.js'))
    following_ids = []
    for follow in following_json:
        if 'following' in follow and 'accountId' in follow['following']:
            following_ids.append(follow['following']['accountId'])
    lookup_users(following_ids, users)
    for id in following_ids:
        handle = users[id].handle if id in users else '~unknown~handle~'
        following.append(handle + ' ' + URL_template_user_id.format(id))
    following.sort()
    with open(paths.file_output_following, 'w', encoding='utf8') as f:
        f.write('\n'.join(following))
    print(f"Wrote {len(following)} accounts to {paths.file_output_following}")


def parse_followers(users, URL_template_user_id, paths):
    """Parse paths.dir_input_data/followers.js, write to paths.file_output_followers.
       Query Twitter API for the missing user handles, if the user agrees.
    """
    followers = []
    follower_json = read_json_from_js_file(os.path.join(paths.dir_input_data, 'follower.js'))
    follower_ids = []
    for follower in follower_json:
        if 'follower' in follower and 'accountId' in follower['follower']:
            follower_ids.append(follower['follower']['accountId'])
    lookup_users(follower_ids, users)
    for id in follower_ids:
        handle = users[id].handle if id in users else '~unknown~handle~'
        followers.append(handle + ' ' + URL_template_user_id.format(id))
    followers.sort()
    with open(paths.file_output_followers, 'w', encoding='utf8') as f:
        f.write('\n'.join(followers))
    print(f"Wrote {len(followers)} accounts to {paths.file_output_followers}")


def chunks(lst: list, n: int):
    """Yield successive n-sized chunks from lst."""
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def parse_direct_messages(username, users, URL_template_user_id, paths):
    """Parse paths.dir_input_data/direct-messages.js, write to one markdown file per conversation.
       Query Twitter API for the missing user handles, if the user agrees.
    """
    # Scan the DMs for missing user handles
    dms_json = read_json_from_js_file(os.path.join(paths.dir_input_data, 'direct-messages.js'))
    dm_user_ids = set()
    for conversation in dms_json:
        if 'dmConversation' in conversation and 'conversationId' in conversation['dmConversation']:
            dm_conversation = conversation['dmConversation']
            conversation_id = dm_conversation['conversationId']
            user1_id, user2_id = conversation_id.split('-')
            dm_user_ids.add(user1_id)
            dm_user_ids.add(user2_id)
    lookup_users(list(dm_user_ids), users)

    # Parse the DMs and store the messages in a dict
    conversations_messages = defaultdict(list)
    for conversation in dms_json:
        if 'dmConversation' in conversation and 'conversationId' in conversation['dmConversation']:
            dm_conversation = conversation['dmConversation']
            conversation_id = dm_conversation['conversationId']
            user1_id, user2_id = conversation_id.split('-')
            messages = []
            if 'messages' in dm_conversation:
                for message in dm_conversation['messages']:
                    if 'messageCreate' in message:
                        message_create = message['messageCreate']
                        if all(tag in message_create for tag in ['senderId', 'recipientId', 'text', 'createdAt']):
                            from_id = message_create['senderId']
                            to_id = message_create['recipientId']
                            body = message_create['text']
                            # replace t.co URLs with their original versions
                            if 'urls' in message_create:
                                for url in message_create['urls']:
                                    if 'url' in url and 'expanded' in url:
                                        expanded_url = url['expanded']
                                        body = body.replace(url['url'], expanded_url)
                            created_at = message_create['createdAt']  # example: 2022-01-27T15:58:52.744Z
                            timestamp = \
                                int(round(datetime.datetime.strptime(created_at, '%Y-%m-%dT%X.%fZ').timestamp()))

                            from_handle = users[from_id].handle.replace('_', '\\_') if from_id in users \
                                else URL_template_user_id.format(from_id)
                            to_handle = users[to_id].handle.replace('_', '\\_') if to_id in users \
                                else URL_template_user_id.format(to_id)

                            message_markdown = f'\n\n### {from_handle} -> {to_handle}: ' \
                                               f'({created_at}) ###\n```\n{body}\n```'
                            messages.append((timestamp, message_markdown))

            # find identifier for the conversation
            other_user_id = user2_id if (user1_id in users and users[user1_id].handle == username) else user1_id

            # collect messages per identifying user in conversations_messages dict
            conversations_messages[other_user_id].extend(messages)

    # output as one file per conversation (or part of long conversation)
    num_written_messages = 0
    num_written_files = 0
    for other_user_id, messages in conversations_messages.items():
        # sort messages by timestamp
        messages.sort(key=lambda tup: tup[0])

        other_user_name = users[other_user_id].handle.replace('_', '\\_') if other_user_id in users \
            else URL_template_user_id.format(other_user_id)

        other_user_short_name: str = users[other_user_id].handle if other_user_id in users else other_user_id

        escaped_username = username.replace('_', '\\_')

        # if there are more than 1000 messages, the conversation was split up in the twitter archive.
        # following this standard, also split up longer conversations in the output files:

        if len(messages) > 1000:
            for chunk_index, chunk in enumerate(chunks(messages, 1000)):
                markdown = ''
                markdown += f'## Conversation between {escaped_username} and {other_user_name}, ' \
                            f'part {chunk_index+1}: ##\n'
                markdown += ''.join(md for _, md in chunk)
                conversation_output_filename = \
                    paths.file_template_dm_output.format(f'{other_user_short_name}_part{chunk_index+1:03}')

                # write part to a markdown file
                with open(conversation_output_filename, 'w', encoding='utf8') as f:
                    f.write(markdown)
                print(f'Wrote {len(chunk)} messages to {conversation_output_filename}')
                num_written_files += 1

        else:
            markdown = ''
            markdown += f'## Conversation between {escaped_username} and {other_user_name}: ##\n'
            markdown += ''.join(md for _, md in messages)
            conversation_output_filename = paths.file_template_dm_output.format(other_user_short_name)

            with open(conversation_output_filename, 'w', encoding='utf8') as f:
                f.write(markdown)
            print(f'Wrote {len(messages)} messages to {conversation_output_filename}')
            num_written_files += 1

        num_written_messages += len(messages)

    print(f"\nWrote {len(conversations_messages)} direct message conversations "
          f"({num_written_messages} total messages) to {num_written_files} markdown files\n")


class PathConfig:
    """Helper class containing constants for various directories and files."""
    def __init__(self, dir_archive, dir_output):
        self.dir_input_data          = os.path.join(dir_archive,           'data')
        self.dir_input_media         = find_dir_input_media(self.dir_input_data)
        self.dir_output_media        = os.path.join(dir_output,            'media')
        self.file_output_following   = os.path.join(dir_output,            'following.txt')
        self.file_output_followers   = os.path.join(dir_output,            'followers.txt')
        self.file_template_dm_output = os.path.join(dir_output,            'DMs-Archive-{}.md')
        self.file_account_js         = os.path.join(self.dir_input_data,   'account.js')
        self.file_download_log       = os.path.join(self.dir_output_media, 'download_log.txt')
        self.file_tweet_icon         = os.path.join(self.dir_output_media, 'tweet.ico')
        self.files_input_tweets      = find_files_input_tweets(self.dir_input_data)


def main():
    paths = PathConfig(dir_archive='.', dir_output='.')

    # Extract the username from data/account.js
    if not os.path.isfile(paths.file_account_js):
        print(f'Error: Failed to load {paths.file_account_js}. Start this script in the root folder of your Twitter archive.')
        exit()
    username = extract_username(paths)

    # URL config
    URL_template_user_id = 'https://twitter.com/i/user/{}'

    html_template = """\
<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link rel="stylesheet"
          href="https://unpkg.com/@picocss/pico@latest/css/pico.min.css">
    <title>Your Twitter archive!</title>
</head>
<body>
    <h1>Your twitter archive</h1>
    <main class="container">
    {}
    </main>
</body>
</html>"""

    users = {}

    # Make a folder to copy the images and videos into.
    os.makedirs(paths.dir_output_media, exist_ok = True)
    if not os.path.isfile(paths.file_tweet_icon):
        shutil.copy('assets/images/favicon.ico', paths.file_tweet_icon);

    media_sources = parse_tweets(username, users, html_template, paths)
    parse_followings(users, URL_template_user_id, paths)
    parse_followers(users, URL_template_user_id, paths)
    parse_direct_messages(username, users, URL_template_user_id, paths)

    # Download larger images, if the user agrees
    print(f"\nThe archive doesn't contain the original-size images. We can attempt to download them from twimg.com.")
    print(f'Please be aware that this script may download a lot of data, which will cost you money if you are')
    print(f'paying for bandwidth. Please be aware that the servers might block these requests if they are too')
    print(f'frequent. This script may not work if your account is protected. You may want to set it to public')
    print(f'before starting the download.')
    user_input = input('\nOK to start downloading? [y/n]')
    if user_input.lower() in ('y', 'yes'):
        download_larger_media(media_sources, paths)
        print('In case you set your account to public before initiating the download, do not forget to protect it again.')


if __name__ == "__main__":
    main()
