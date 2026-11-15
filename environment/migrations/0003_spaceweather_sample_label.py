from django.db import migrations, models


LEGACY_SAMPLE_SUMMARIES = (
    'Major X1.8 solar flare erupting from active sunspot region AR3664. Associated with R3 strong HF radio blackout.',
    'G2 moderate geomagnetic storm resulting from halo CME arrival. Interplanetary magnetic field southward tilt observed.',
    'Partial halo coronal mass ejection observed by SOHO/LASCO C3 coronagraph. Earth/Mars transit trajectory.',
    'GOES-16 detected energetic proton flux exceeding 100 pfu threshold. Biological astronaut hazard active outside magnetosphere.',
)


def label_legacy_sample_events(apps, schema_editor):
    SpaceWeatherEvent = apps.get_model('environment', 'SpaceWeatherEvent')
    SpaceWeatherEvent.objects.filter(summary__in=LEGACY_SAMPLE_SUMMARIES).update(is_sample=True)
    SpaceWeatherEvent.objects.filter(
        event_type__in=('cme', 'solar_particle')
    ).update(severity='unclassified')


class Migration(migrations.Migration):

    dependencies = [
        ('environment', '0002_spaceweatherevent'),
    ]

    operations = [
        migrations.AddField(
            model_name='spaceweatherevent',
            name='is_sample',
            field=models.BooleanField(
                default=False,
                help_text='True for locally generated sample data; false for NASA DONKI records.',
            ),
        ),
        migrations.AlterField(
            model_name='spaceweatherevent',
            name='severity',
            field=models.CharField(
                choices=[
                    ('low', 'Low'),
                    ('moderate', 'Moderate'),
                    ('high', 'High'),
                    ('unclassified', 'Unclassified'),
                ],
                db_index=True,
                default='low',
                max_length=20,
            ),
        ),
        migrations.RunPython(label_legacy_sample_events, migrations.RunPython.noop),
    ]
