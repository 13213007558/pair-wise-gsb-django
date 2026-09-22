from django.db import models

CUSTOM_ON_DELETE_CALLS = []
CALLABLE_SET_CALLS = []


def custom_on_delete(collector, field, sub_objs, using):
    CUSTOM_ON_DELETE_CALLS.append((field.name, [obj.pk for obj in sub_objs]))
    collector.collect(sub_objs)


def callable_set_value():
    CALLABLE_SET_CALLS.append(())
    return "done"


class Author(models.Model):
    name = models.CharField(max_length=100)


class Series(models.Model):
    title = models.CharField(max_length=100)
    author = models.ForeignKey(
        Author, models.CASCADE, null=True, related_name="series"
    )


class Tag(models.Model):
    label = models.CharField(max_length=100)


class Book(models.Model):
    author = models.ForeignKey(Author, models.CASCADE, related_name="books")
    series = models.ForeignKey(
        Series, models.RESTRICT, null=True, related_name="books"
    )
    tags = models.ManyToManyField(Tag, related_name="books")


class Award(models.Model):
    """Reachable through two CASCADE paths (author and book)."""

    author = models.ForeignKey(Author, models.CASCADE, related_name="awards")
    book = models.ForeignKey(Book, models.CASCADE, related_name="awards")


class Shelf(models.Model):
    owner = models.ForeignKey(
        Author, models.SET_NULL, null=True, related_name="shelves"
    )


class Profile(models.Model):
    author = models.ForeignKey(Author, models.PROTECT, related_name="profiles")


class Status(models.Model):
    code = models.CharField(max_length=20, primary_key=True)


class ConstantSetTask(models.Model):
    status = models.ForeignKey(
        Status, models.SET("done"), null=True, related_name="constant_set_tasks"
    )


class CallableSetTask(models.Model):
    status = models.ForeignKey(
        Status, models.SET(callable_set_value), null=True, related_name="+"
    )


class CustomOnDeleteTask(models.Model):
    status = models.ForeignKey(
        Status, custom_on_delete, null=True, related_name="+"
    )


class Place(models.Model):
    name = models.CharField(max_length=100)


class Restaurant(Place):
    owner = models.ForeignKey(Author, models.CASCADE, related_name="restaurants")


class Node(models.Model):
    parent = models.ForeignKey(
        "self", models.CASCADE, null=True, related_name="children"
    )
