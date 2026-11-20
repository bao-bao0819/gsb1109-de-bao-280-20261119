from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0018_author_resource"),
    ]

    operations = [
        migrations.CreateModel(
            name="Supplier",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("order_no", models.CharField(max_length=32)),
                ("store_no", models.CharField(max_length=32)),
            ],
        ),
    ]
