import os
from peewee import Model, ForeignKeyField, SqliteDatabase, CharField, TextField, IntegerField


database = SqliteDatabase("udemy-dl.db")


class BaseModel(Model):
    class Meta:
        database = database


class Course(BaseModel):
    course_id = CharField(unique=True)
    course_slug = CharField(unique=True)
    course_name_string = TextField()


class Chapter(BaseModel):
    chapter_id = CharField(unique=True)
    chapter_index = IntegerField(default=0)
    course = ForeignKeyField(Course, backref="chapter")
    description = TextField(default="")
    title = TextField(default="")


class Lecture(BaseModel):
    lecture_id = CharField(unique=True)
    chapter = ForeignKeyField(Chapter, backref="lecture")
    title = TextField(default="")
    lecture_index = IntegerField()
    lecture_type = CharField(default="")
    content = TextField(default="")


class LectureSupplementAsset(BaseModel):
    asset_id = CharField(unique=True)
    lecture = ForeignKeyField(Lecture, backref="supplementasset")
    title = TextField(default="")
    type_name = CharField(default="")
    saved_path = CharField(default="")


class LectureVideoAsset(BaseModel):
    lecture = ForeignKeyField(Lecture, backref="lecturevideoasset")
    subtitles = CharField(default="")
    saved_path = CharField(default="")


def create_tables():
    with database:
        database.create_tables(
            [Course, Chapter, Lecture, LectureSupplementAsset, LectureVideoAsset],
            safe=True)
