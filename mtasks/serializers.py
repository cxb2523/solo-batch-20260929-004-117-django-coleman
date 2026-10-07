from django.contrib.auth.models import User
from rest_framework import serializers, viewsets
from django.core.exceptions import ValidationError as DjangoValidationError

from partner.serializers import PartnerSerializer
from .models import (
    Task,
    validate_task_title,
    VALIDATION_SOURCE_CLEAN,
    VALIDATION_SOURCE_SERIALIZER_FIELDS,
)


class UserSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = User
        fields = (
            'username',
            'first_name',
            'last_name',
        )


class TaskSerializer(serializers.HyperlinkedModelSerializer):
    partner = PartnerSerializer(read_only=True)
    user = UserSerializer(read_only=True)

    class Meta:
        model = Task
        fields = (
            'number',
            'title',
            'partner',
            'user',
            'description',
            'resolution',
            'deadline',
            'state',
            'created_at',
        )

    # Only FIELD-LEVEL validation lives here, and it reuses the same rule
    # functions defined in models.py. Every cross-field rule (duplicate
    # title, state transition edges...) lives in Task.clean and is reused
    # through validate(), so the two layers can never disagree.
    def validate_title(self, value):
        try:
            return validate_task_title(value)
        except DjangoValidationError as err:
            raise serializers.ValidationError(err.messages)

    def validate(self, attrs):
        instance = self.instance or Task()
        for field_name, value in attrs.items():
            setattr(instance, field_name, value)
        try:
            instance.clean()
        except DjangoValidationError as err:
            errors = getattr(err, 'message_dict', None)
            if errors is None:
                errors = {'non_field_errors': err.messages}
            raise serializers.ValidationError(errors)
        attrs['_validation_sources'] = [
            VALIDATION_SOURCE_SERIALIZER_FIELDS,
            VALIDATION_SOURCE_CLEAN,
        ]
        return attrs

    def create(self, validated_data):
        sources = validated_data.pop('_validation_sources', None)
        instance = Task(**validated_data)
        if sources is not None:
            instance.validation_sources = sources
        instance.save()
        return instance

    def update(self, instance, validated_data):
        sources = validated_data.pop('_validation_sources', None)
        if sources is not None:
            instance.validation_sources = sources
        for field_name, value in validated_data.items():
            setattr(instance, field_name, value)
        instance.save()
        return instance


class TaskViewSet(viewsets.ModelViewSet):
    queryset = Task.objects.all()
    serializer_class = TaskSerializer
