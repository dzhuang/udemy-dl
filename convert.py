# -*- coding: utf-8 -*-

import os
import sys
import jinja2
import re
from peewee import SqliteDatabase, OperationalError
from udemy.models import (
    Chapter, LectureVideoAsset, Lecture, LectureSupplementAsset, Course)
from django.conf.global_settings import LANGUAGES
from bs4 import BeautifulSoup as BeautifulSoup_
from qiniu import Auth, put_file, etag, BucketManager
from qiniu import build_batch_move

LOCAL_PATH_PREFIX = os.getcwd()

database = SqliteDatabase(os.path.join(LOCAL_PATH_PREFIX, "udemy-dl.db"))

upload_to_qiniu = False
QINIU_ACCESS_KEY = os.environ.get("QINIU_ACCESS_KEY", "")
QINIU_SECRET_KEY = os.environ.get("QINIU_SECRET_KEY", "")
QINIU_BUCKET_NAME = os.environ.get("QINIU_BUCKET_NAME", "")
QINIU_BUCKET_VIDEO_NAME = os.environ.get("QINIU_BUCKET_VIDEO_NAME", "")

qiniu_auth = None

if (not sys.platform.startswith("win")
        and QINIU_ACCESS_KEY and QINIU_SECRET_KEY and QINIU_BUCKET_NAME and QINIU_BUCKET_VIDEO_NAME):
    upload_to_qiniu = True
    qiniu_auth = Auth(QINIU_ACCESS_KEY, QINIU_SECRET_KEY)

flow_template = u"""
title: "{{ module_name }}"
description: |
{% if module_description %}
    <div class="well">
    {{ module_description |indent(width=4)}}
    </div>
{% endif %}

rules:
    access:
    -
        if_has_role: [student, ta, instructor]
        permissions: [view]

    grade_identifier: null

pages:

{% for page in pages %}
-
    type: Page
    id: udemy_{{ page.id }}
    content: |
        # {{ page.title|safe }}

        {{ page.content |indent(width=8)|safe }}

{% endfor %}
"""

video_template = """
<video class="video-js vjs-default-skin vjs-fluid vjs-big-play-centered" controls preload="none" data-setup='[]' playsinline>
  <source src='mooc-udemy:{{ video.url }}' type='video/mp4' />
  {% for subtitle in video.subtitles %}<track kind='captions' src='mooc-udemy:{{ subtitle.url }}' srclang='{{ subtitle.lang }}' label='{{ subtitle.lang_name}}' {% if subtitle.is_default %} default {% endif %} />
  {% endfor %}
</video>
"""

resource_template = """
<hr>

{% raw %}{% from "macros.jinja" import downloadviewpdf %}{% endraw %}

<h3>Resources</h3>
<ul>{% for asset in assets %}
  <li>{% if asset.is_pdf %}{% raw %}{{ downloadviewpdf("{% endraw %}{{asset.url}}{% raw %}", "{% endraw %}{{asset.name}}{% raw %}")}}{% endraw %}{% else %}
  <a href="mooc-udemy:{{asset.url}}" target="_blank">{{asset.name}}</a>{% endif %}</li>{% endfor %}
</ul>

"""

course_chunks_template_embed = """
-
    title: "Course: {{ course.course_name_string }}"
    id: {{ course.course_slug }}
    collapsible: True

    content: |    
        ## {{ course.course_name_string }}
        
        {% raw %}
        {% from "macros.jinja" import accordion, button, file %}
        {% endraw %}
        
        {% for flow in flows %}
        #### Module {{loop.index}}: {{ flow.name }} {% raw -%}{{ button("flow:{%- endraw -%}{{flow.flow_id}}{%- raw -%}") }}{%- endraw %}
        
        {{ flow.description }}
        
        <hr>
        
        {% endfor %}
"""

course_chunks_template_single = """
chunks:

- 
    title: "{{ course.course_name_string }}"
    id: toc
    content: |
    

{% for flow in flows %}
-
    title: "Module {{loop.index}}: {{ flow.name }}"
    id: {{course.course_slug|replace("-", "_")}}_module_{{loop.index}}
    collapsible: True

    content: |    
        {% raw %}
        {% from "macros.jinja" import accordion, button, file %}
        {% endraw %}

        #### Module {{loop.index}}: {{ flow.name }} {% raw -%}{{ button("flow:{%- endraw -%}{{flow.flow_id}}{%- raw -%}") }}{%- endraw %}

        {{ flow.description|indent(width=8) }}

        <hr>

{% endfor %}
"""


def BeautifulSoup(page): return BeautifulSoup_(page, 'html.parser')


class UdemyPage(object):
    def __init__(self, id, title, content):
        self.id = id.replace("-", "_")
        self.title = title
        self.content = content.replace("\t", " ")


class UdemyVideoSubtitle(object):
    def __init__(self, url, lang, is_default=False):
        self.url = url
        self.lang = lang
        self.lang_name = self.get_lang_name()
        self.is_default = is_default

    def get_lang_name(self):
        maps = {'zh-CN': 'zh-hans', 'zh-TW': 'zh-hant'}
        lang = maps.get(self.lang, self.lang).lower()
        return dict(LANGUAGES).get(lang, "English")

    def __repr__(self):
        return "%s(%s)" % (self.url, self.lang_name)


class UdemyLectureAsset(object):
    def __init__(self, name, saved_path):
        self.url = local_path_to_url(saved_path)
        self.name = name
        self.is_pdf = bool(self.url.lower().endswith(".pdf"))


class UdemyVideo(object):
    def __init__(self, url, langs=None):
        self.url = url

        self.subtitles = []
        if langs:
            for i, lang in enumerate(langs):
                is_default = False
                if i == 0:
                    is_default = True
                self.subtitles.append(
                    UdemyVideoSubtitle(self.get_subtitle_url(lang), lang, is_default))

    def __repr__(self):
        return "%s(%s)" % (self.url, ",".join(str(sub) for sub in self.subtitles))

    def get_subtitle_url(self, lang):
        return replace_ext(self.url, ext=".%s.vtt" % lang)


def replace_ext(path, ext):
    if ext and not ext.startswith("."):
        ext = ".%s" % ext

    return os.path.splitext(path)[0] + ext


def local_path_to_url(local_path, ext=None):
    if ext and not ext.startswith("."):
        ext = ".%s" % ext

    if ext:
        local_path = os.path.splitext(local_path)[0] + ext

    from six.moves.urllib.parse import urljoin
    if sys.platform.startswith("win"):
        assert local_path.startswith(LOCAL_PATH_PREFIX), local_path

        striped_local_path = local_path[len(LOCAL_PATH_PREFIX):]
        striped_local_path = striped_local_path.replace("\\", "/")
    else:
        assert os.path.isfile(os.path.join(os.getcwd(), local_path))
        local_path = os.path.relpath(local_path, start=os.getcwd())
        upload_resource_to_qiniu(local_path)
        striped_local_path = local_path
    assert not striped_local_path.startswith("/")
    return striped_local_path


def convert_video_page(database, video_lecture):
    with database:
        video_assets = LectureVideoAsset.select().join(Lecture).where(
            Lecture.lecture_id == video_lecture.lecture_id)

    assert len(video_assets) <= 1

    if not len(video_assets):
        return

    video_asset = video_assets[0]
    url = local_path_to_url(video_asset.saved_path)
    sub_list = [lang.strip() for lang in video_asset.subtitles.split(",") if lang.endswith(".vtt")]
    langs = []
    for lang in ['zh-CN', 'zh-TW', 'en']:
        if lang + ".vtt" in sub_list:
            langs.append(lang)
            upload_resource_to_qiniu(replace_ext(video_asset.saved_path, ext=".%s.vtt" % lang))

    for sub in sub_list:
        lang, _ = os.path.splitext(sub)
        if lang not in langs:
            langs.append(lang)

    video = UdemyVideo(url=url, langs=langs)

    jinja_env = jinja2.Environment()
    template = jinja_env.from_string(video_template)
    video_html = template.render(video=video)

    return video_html


COLON_START = re.compile(r'\n\s*:', re.M)


def avoid_colon_at_beginning(s):
    s = re.sub(COLON_START, ":", s)
    return s


def convert_normal_page(database, lecture):
    content = avoid_colon_at_beginning(lecture.content)
    soup = BeautifulSoup(content)

    # Remove header tag if its content is the same with the title.
    for header_name in ["h1", "h2", "h3", "h4"]:
        header_tags = soup.find(header_name)
        if header_tags:
            try:
                header_tag_content = " ".join([str(content) for content in header_tags.contents])
            except Exception as e:
                raise e
            else:
                header_tag_content = header_tag_content.replace("\n", " ").replace("  ", " ")
                header_tag_content = header_tag_content.strip()
                if header_tag_content == lecture.title:
                    header_tags.decompose()

    resource_html = ""
    with database:
        supplements = LectureSupplementAsset.select().join(Lecture).where(
                Lecture.lecture_id == lecture.lecture_id)

    if len(supplements):
        jinja_env = jinja2.Environment()
        template = jinja_env.from_string(resource_template)

        assets = []
        for asset in supplements:
            if asset.saved_path:
                assets.append(UdemyLectureAsset(asset.title, asset.saved_path))

        resource_html = template.render(assets=assets)

    return "\n".join([soup.decode_contents(), resource_html])


def generate_flow(chapter_id, ordinal):
    with database:
        chapter = Chapter.get(chapter_id=chapter_id)
        lectures = Lecture.select().join(Chapter).where(Chapter.chapter_id == chapter_id)

    course_slug = chapter.course.course_slug
    slug = "%s_%s_%s" % (course_slug, str(ordinal), chapter_id)

    flow_id = slug.replace("_", "-")
    yaml_path = "%s.yml" % flow_id
    file_name = os.path.join(os.getcwd(), yaml_path)

    pages = []
    for i, lecture in enumerate(lectures):
        if lecture.lecture_type.lower() == "video":
            content = convert_video_page(database, lecture)
        else:
            content = convert_normal_page(database, lecture)

        pages.append(UdemyPage(id="%s_%s" % (str(lecture.lecture_id), str(i + 1)), title=lecture.title, content=content))

    jinja_env = jinja2.Environment()
    template = jinja_env.from_string(flow_template)
    output = template.render(module_name=chapter.title, module_description=chapter.description, pages=pages)

    if sys.platform.startswith("win"):
        with open(file_name, "w", encoding="utf-8") as f:
            f.write(output)

    upload_yml_to_dropbox("/" + os.path.join(course_slug, "flows", yaml_path), output.encode())
    sys.stdout.write("---%s uploaded to Dropbox.---\n" % flow_id)
    return flow_id


class UdemyFlow(object):
    def __init__(self, name, flow_id, description=""):
        self.name = name
        self.flow_id = flow_id
        self.description = description


def generate_yamls(course_slug):
    with database:
        course = Course.get(course_slug=course_slug)
        chapters = Chapter.select().join(Course).where(Course.course_slug == course_slug)

    flows = []
    for i, chapter in enumerate(chapters):
        flow_id = generate_flow(chapter.chapter_id, i + 1)
        flows.append(UdemyFlow(chapter.title, flow_id, description=chapter.description))

    def generate_course_yml(template_name, yaml_path):
        jinja_env = jinja2.Environment()
        template = jinja_env.from_string(template_name)
        output = template.render(course=course, flows=flows)

        if sys.platform.startswith("win"):
            with open(yaml_path, "w", encoding="utf-8") as f:
                f.write(output)
                return

        dropbox_path = "/" + os.path.join(course_slug, yaml_path)
        upload_yml_to_dropbox(dropbox_path, output.encode())

    # for embedded chunk
    yaml_path = "%s_course_chunks.yml" % course_slug.replace("_", "-")
    template_name = course_chunks_template_embed
    generate_course_yml(template_name, yaml_path)

    # for single course
    yaml_path = "course.yml"
    template_name = course_chunks_template_single
    generate_course_yml(template_name, yaml_path)

    sys.stdout.write("--------------Done!-----------------\n")


def tqdmWrapViewBar(*args, **kwargs):
    from tqdm import tqdm
    pbar = tqdm(*args, **kwargs)  # make a progressbar
    last = [0]  # last known iteration, start at 0
    def viewBar(a, b):
        pbar.total = int(b)
        pbar.update(int(a - last[0]))  # update pbar with increment
        last[0] = a  # update last known iteration
    return viewBar, pbar  # return callback, tqdmInstance


def upload_yml_to_dropbox(file_name, file_content):
    if sys.platform.startswith("win"):
        return
    dropbox_token = os.environ.get("DROPBOX_ACCESS_TOKEN", "")
    if not dropbox_token:
        return

    import dropbox
    from dropbox.files import WriteMode
    dbx = dropbox.Dropbox(dropbox_token)
    return dbx.files_upload(file_content, file_name, mode=WriteMode.overwrite)


def _upload_resource_to_qiniu(bucket_name, file_path, prefix="udemy-videos"):
    file_etag = etag(file_path)
    bucket = BucketManager(qiniu_auth)

    # strip local (absolute) path to relative path
    file_path = os.path.relpath(file_path, start=os.getcwd())
    qiniu_file_path = os.path.join(prefix, file_path)
    ret, _ = bucket.stat(bucket_name, qiniu_file_path)

    # Check if the file exists / changed, if not, upload or update.
    if ret and "hash" in ret:
        if file_etag == ret["hash"]:
            sys.stdout.write("File with hash '%s' already exist.\n" % file_etag)
            return
        else:
            sys.stdout.write(
                "File with hash '%s' changed, will be overwritten.\n" % ret["hash"])

    size = os.stat(file_path).st_size / 1024 / 1024
    sys.stdout.write("Uploading file with hash %s (size: %.1fM)\n" % (file_etag, size))
    token = qiniu_auth.upload_token(bucket_name, qiniu_file_path, 3600)

    cbk, pbar = tqdmWrapViewBar(ascii=True, unit='b', unit_scale=True)
    put_file(token, qiniu_file_path, file_path, progress_handler=cbk)
    pbar.close()


def upload_resource_to_qiniu(file_path):
    if not qiniu_auth or not upload_to_qiniu:
        return

    if file_path.endswith(".mp4"):
        return _upload_resource_to_qiniu(QINIU_BUCKET_VIDEO_NAME, file_path)
    else:
        return _upload_resource_to_qiniu(QINIU_BUCKET_NAME, file_path)


def main():
    try:
        with database:
            courses = Course.select()
            course_slugs = [course.course_slug for course in courses]
        for slug in course_slugs:
            generate_yamls(slug)

    except OperationalError as e:
        if "no such table" in str(e):
            sys.stdout.write("Warning: No Course was downloaded.")
        else:
            raise e


if __name__ == "__main__":
    main()
