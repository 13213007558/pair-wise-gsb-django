from django.db import models


class Store(models.Model):
    name = models.CharField(max_length=20, null=True)


class Sale(models.Model):
    store = models.ForeignKey(Store, models.CASCADE, null=True)
    region = models.CharField(max_length=20, null=True)
    category = models.CharField(max_length=20, null=True)
    amount = models.IntegerField()
