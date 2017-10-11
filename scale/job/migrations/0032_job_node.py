# -*- coding: utf-8 -*-
# Generated by Django 1.11.2 on 2017-09-26 19:29
from __future__ import unicode_literals

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('node', '0004_auto_20170524_1639'),
        ('job', '0031_auto_20170822_1544'),
    ]

    operations = [
        migrations.AddField(
            model_name='job',
            name='node',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to='node.Node'),
        ),
    ]
