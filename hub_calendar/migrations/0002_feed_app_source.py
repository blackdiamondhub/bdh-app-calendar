from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("hub_calendar", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="feed",
            name="source",
            field=models.CharField(choices=[("url", "Link"), ("app", "From an app")], default="url", max_length=8),
        ),
        migrations.AlterField(
            model_name="feed",
            name="url",
            field=models.URLField(blank=True, default="", max_length=1000),
        ),
        migrations.AddField(
            model_name="feed",
            name="provider",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="feed",
            name="share_name",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="feed",
            name="share_revision",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="feed",
            name="attachments",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
