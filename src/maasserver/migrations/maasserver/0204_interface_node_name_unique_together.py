# -*- coding: utf-8 -*-
# Generated by Django 1.11.11 on 2020-01-14 21:55
from __future__ import unicode_literals

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("maasserver", "0203_interface_node_name_duplicates_delete")
    ]
    operations = [
        migrations.AlterUniqueTogether(
            name="interface", unique_together=set([("node", "name")])
        )
    ]