from django.contrib import admin

from companion.models import CompanionMemory, Conversation, Message

admin.site.register(Conversation)
admin.site.register(Message)
admin.site.register(CompanionMemory)
