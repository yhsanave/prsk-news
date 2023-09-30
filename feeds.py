from discordwebhook import Discord
from github import Github, Auth
from datetime import datetime
from dataclasses import dataclass, asdict
import html2text
import requests
import config
import json
import re
import pytz

ghAuth = Auth.Token(config.G_ACCESS_TOKEN)
gh = Github(auth=ghAuth)
repo = gh.get_repo(config.G_REPO_PATH)

htmlParser = html2text.HTML2Text(baseurl=config.INFO_BASE_URL)
htmlParser.single_line_break = True
htmlParser.protect_links = True
htmlParser.images_as_html = True
htmlParser.body_width = 0

IMAGE_PATTERN = re.compile(r"<img.*?src='(.*)'.*?>")

DATETIME_MASTER_PATTERN = re.compile(
    r'(?P<date>(?P<month>[A-Z][a-z]+)\.? (?P<day>\d{1,2})[,: ]? ?(?P<year>\d{4})?)(?:(?:[.,]| at) ?)(?P<time>(?P<hour>\d{2}):(?P<minute>\d{2})(?:[AP]M)? \(?(?P<zone>[A-Z]{3,4})\)?)?')
DATETIME_LIST_PATTERN = re.compile(
    r'(?P<month>[A-Z][a-z]+)\.? (?P<day>\d{1,2}): (?P<times>(?:\d{2}:\d{2}(?:[AP]M)? \(?[A-Z]{3,4}\),? ?)+)')
TIME_PATTERN = re.compile(
    r'(?P<hour>\d{2}):(?P<minute>\d{2})(?:[AP]M)?(?: \(?[A-Z]{3,4}\))?,? ?')

NEWS_COLOR_MAP = {
    'bug': 10066329,
    'campaign': 16733611,
    'event': 16733611,
    'gacha': 16733611,
    'information': 52411,
    'music': 16755200,
    'update': 16733577
}

with open('log.json', 'r') as f:
    feedLogs = json.load(f)


@dataclass
class PostArgs:
    content: str | None = None
    username: str | None = None
    avatar_url: str | None = None
    embeds: list[dict] | None = None


class DictObj:
    def __init__(self, in_dict: dict):
        assert isinstance(in_dict, dict)
        for key, val in in_dict.items():
            if isinstance(val, (list, tuple)):
                setattr(self, key, [DictObj(x) if isinstance(
                    x, dict) else x for x in val])
            else:
                setattr(self, key, DictObj(val)
                        if isinstance(val, dict) else val)


class FeedEntry(DictObj):
    id: int

    def __init__(self, in_dict: dict):
        super().__init__(in_dict)

    def build_post(self):
        raise NotImplementedError


class NewsEntry(FeedEntry):
    id: int
    seq: int
    informationType: str
    informationTag: str
    browseType: str
    platform: str
    title: str
    path: str
    startAt: int
    endAt: int

    urlPath: str = None
    imageURL: str = None

    def __init__(self, in_dict: dict):
        super().__init__(in_dict)

        if self.path.startswith('information'):
            self.urlPath = config.INFO_BASE_URL + self.path
            self.htmlPath = config.INFO_HTML_URL + \
                self.path[self.path.find('?id=')+4:] + '.html'

    def build_post(self):
        return asdict(PostArgs(
            content='New in-game news posted!',
            embeds=[
                {
                    "title": self.title,
                    "description": self.get_body() if self.browseType == 'internal' else None,
                    "url": self.urlPath if self.urlPath else self.path,
                    "image": {"url": self.imageURL},
                    "color": NEWS_COLOR_MAP.get(self.informationTag, None)
                }
            ]
        ))

    def get_body(self):
        raw = requests.get(self.htmlPath)
        text = htmlParser.handle(raw.text)
        self.process_images(IMAGE_PATTERN.findall(text))
        text = text.replace('â', '## ■').replace(
            '* * *', '').replace('â', '★').replace('â¢', '* ').replace('\n-', '\n* ')
        text = re.sub(IMAGE_PATTERN, '', text)
        text = self.process_datetimes(text)
        return text

    def process_images(self, imageURLs: list[str]):
        if imageURLs:
            self.imageURL = config.INFO_BASE_URL[:-1] + imageURLs[0]

    def process_datetimes(self, text: str):
        text = re.sub(DATETIME_MASTER_PATTERN, DateHandler.handle_single, text)
        text = re.sub(DATETIME_LIST_PATTERN, DateHandler.handle_list, text)
        return text


class EventEntry(FeedEntry):
    id: int
    eventType: str
    name: str
    assetbundleName: str
    bgmAssetbundleName: str
    startAt: int
    aggregateAt: int
    rankingAnnounceAt: int
    distributionStartAt: int
    closedAt: int
    distributionEndAt: int
    virtualLiveId: int
    eventRankingRewardRanges: list


class GachaEntry(FeedEntry):
    id: int
    gachaType: str
    name: str
    seq: int
    assetbundleName: str
    rarity1Rate: int
    rarity2Rate: int
    rarity3Rate: int
    rarity4Rate: int
    startAt: int
    endAt: int
    gachaCeilItemId: int
    gachaCardRarityRates: list
    gachaDetails: list
    gachaBehaviors: list
    gachaPickups: list
    gachaPickupCostumes: list
    gachaInformation: dict


class Feed:
    name: str = ''
    webhookUrl: str
    githubPath: str

    webhook: Discord

    lastChecked: float = 0
    posted: list[int] = []
    lastModified: float = 0

    entryType = FeedEntry
    feed: list[FeedEntry] = []

    def __init__(self, webhookUrl: str, githubPath: str) -> None:
        self.webhookUrl = webhookUrl
        self.githubPath = githubPath

        self.webhook = Discord(url=self.webhookUrl)

        self.load_from_log()
        self.feed = self.get_feed()

    def load_from_log(self):
        if not self.name in feedLogs:
            print(f'No log found for feed {self.name}, using default values')
            return

        self.lastChecked = feedLogs[self.name]['lastChecked']
        self.posted = feedLogs[self.name]['posted']

    def get_feed(self):
        contents = repo.get_contents(path=self.githubPath)
        self.lastModified = datetime.strptime(
            contents.last_modified, r'%a, %d %b %Y %H:%M:%S %Z').timestamp()

        if self.lastModified > self.lastChecked or True:
            feed = self.parse_feed(json.loads(contents.decoded_content))
        else:
            print(f'Skipping feed {self.name}. File has not been modified.')
            feed = []

        self.lastChecked = datetime.now().timestamp()
        return feed

    def parse_feed(self, feed: list):
        return [self.entryType(entry) for entry in feed]

    def post_feed(self, maxPosts: int = 1):
        postCount = 0
        for entry in [e for e in self.feed if e.id not in self.posted]:
            if postCount >= maxPosts:
                break

            self.post(entry)
            postCount += 1

        self.write_logs()

    def write_logs(self):
        feedLogs[self.name] = {
            'lastChecked': self.lastChecked,
            'posted': self.posted
        }

    def post(self, entry: FeedEntry):
        try:
            self.webhook.post(**entry.build_post())
            self.posted.append(entry.id)
        except Exception as e:
            print(f'Failed to post entry {entry.id}', e)


class NewsFeed(Feed):
    name = 'news'
    entryType = NewsEntry


class EventFeed(Feed):
    name = 'event'
    entryType = EventEntry


class GachaFeed(Feed):
    name = 'gacha'
    entryType = GachaEntry


class DateHandler:
    MONTH_MAP = {
        'Jan': 1,
        'Feb': 2,
        'Mar': 3,
        'Apr': 4,
        'May': 5,
        'Jun': 6,
        'Jul': 7,
        'Aug': 8,
        'Sep': 9,
        'Oct': 10,
        'Nov': 11,
        'Dec': 12
    }

    def handle_single(match: re.Match):
        data = {
            'month': DateHandler.MONTH_MAP.get(match.group('month')[:3], None) if match.group('month') else datetime.now().month,
            'day': int(match.group('day')) if match.group('day') else 1,
            'year': int(match.group('year')) if match.group('year') else datetime.now().year,
            'hour': int(match.group('hour')) if match.group('hour') else 0,
            'minute': int(match.group('minute')) if match.group('minute') else 0,
            'second': 0
        }
        dt = DateHandler.timezone_converter(
            datetime(**data), config.REGION_TIME_ZONE)
        return DateHandler.make_timestamp(dt)

    def handle_list(match: re.Match):
        data = {
            'year': datetime.today().year,
            'month': DateHandler.MONTH_MAP.get(match.group('month')[:3], None) if match.group('month') else 1,
            'day': int(match.group('day')) if match.group('day') else 1
        }

        return f'{match.group("month")} {data["day"]}: {", ".join([DateHandler.list_repl(**data, hour=int(t[0]), minute=int(t[1])) for t in re.findall(TIME_PATTERN, match.group("times"))])}'

    def list_repl(year: int, month: int, day: int, hour: int, minute: int):
        dt = DateHandler.timezone_converter(datetime(
            year=year, month=month, day=day, hour=hour, minute=minute, second=0), config.REGION_TIME_ZONE)
        return DateHandler.make_timestamp(dt, 't')

    def make_timestamp(date_time: datetime, display: str = 'f'):
        return f'<t:{int(date_time.timestamp())}:{display}>'

    def timezone_converter(input_dt, current_tz='US/Pacific', target_tz='UTC'):
        current_tz = pytz.timezone(current_tz)
        target_tz = pytz.timezone(target_tz)
        target_dt = current_tz.localize(input_dt).astimezone(target_tz)
        return target_tz.normalize(target_dt)


if __name__ == '__main__':
    news = NewsFeed(webhookUrl=config.D_NEWS_WEBHOOK,
                    githubPath=config.G_NEWS_PATH)
    news.post_feed()
    with open('log.json', 'w') as f:
        json.dump(feedLogs, f)
