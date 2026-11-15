from django.urls import path

from companion import views

app_name = 'companion'

urlpatterns = [
    path('history/', views.history_view, name='history'),
    path('send/', views.send_view, name='send'),
    path('new/', views.new_conversation_view, name='new'),
    path('memories/', views.memories_view, name='memories'),
    path('memories/add/', views.memory_add_view, name='memory_add'),
    path('memories/<int:memory_id>/delete/', views.memory_delete_view, name='memory_delete'),
]
