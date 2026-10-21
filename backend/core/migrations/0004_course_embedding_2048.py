from django.db import migrations, models
import pgvector.django.vector


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0003_agentattachment"),
    ]

    operations = [
        migrations.AlterField(
            model_name="chunkembedding",
            name="embedding",
            field=pgvector.django.vector.VectorField(blank=True, dimensions=2048, null=True),
        ),
        migrations.AlterField(
            model_name="chunkembedding",
            name="dimensions",
            field=models.PositiveIntegerField(default=2048),
        ),
    ]
