from django.db import models


class Sale(models.Model):
    region = models.CharField(max_length=20, null=True)
    category = models.CharField(max_length=20, null=True)
    amount = models.IntegerField(null=True)
    quantity = models.IntegerField(null=True)
