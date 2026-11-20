from django.db import models
from simple_history.models import HistoricalRecords


class Product(models.Model):
    """离线迁移商品主表（回放专用小表，sku 唯一，模拟线上商品表约束）。"""

    sku = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=100)
    price = models.IntegerField(default=0)
    source_batch = models.CharField(max_length=32)

    class Meta:
        app_label = "replay_app"

    history = HistoricalRecords()
