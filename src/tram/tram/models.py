import os

from constance import config
from django.contrib.auth.models import User
from django.core.files import File
from django.db import models
from django.db.models import Count, Q
from django.db.models.signals import post_delete
from django.dispatch.dispatcher import receiver

DISPOSITION_CHOICES = (
    ('accept', 'Accepted'),
    ('reject', 'Rejected'),
    (None, 'No Disposition'),
)

JOB_STATUS_CHOICES = (
    ('queued', 'Queued'),
    ('error', 'Error'),
)

SENTENCE_PREVIEW_CHARS = 40


class AttackTechnique(models.Model):
    """Attack Techniques
    """
    name = models.CharField(max_length=200)
    stix_id = models.CharField(max_length=128, unique=True)
    attack_id = models.CharField(max_length=128, unique=True)
    attack_url = models.CharField(max_length=512)
    matrix = models.CharField(max_length=200)
    created_on = models.DateTimeField(auto_now_add=True)
    updated_on = models.DateTimeField(auto_now=True)

    sentences = models.ManyToManyField('Sentence', through='SentenceTechniqueMapping')

    @classmethod
    def get_sentence_counts(cls, accept_threshold=0):
        """
        accept_threshold - Only return AttackTechniques where accepted_sentences >= accept_threshold
        return: The list of AttackTechnique objects, annotated with how many training sentences
                have been accepted, pending, and there are in total.
        """
        sentence_counts = cls.objects.annotate(
            accepted_sentences=Count('sentences', filter=Q(sentences__disposition='accept')),
            pending_sentences=Count('sentences', filter=Q(sentences__disposition=None)),
            total_sentences=Count('sentences')
        ).order_by('-accepted_sentences', 'attack_id').filter(accepted_sentences__gte=accept_threshold)
        return sentence_counts

    def __str__(self):
        return '(%s) %s' % (self.attack_id, self.name)


class AttackGroup(models.Model):
    """Attack Groups
    """
    name = models.CharField(max_length=200)
    stix_id = models.CharField(max_length=128, unique=True)
    attack_id = models.CharField(max_length=128, unique=True)
    attack_url = models.CharField(max_length=512)
    matrix = models.CharField(max_length=200)
    created_on = models.DateTimeField(auto_now_add=True)
    updated_on = models.DateTimeField(auto_now=True)

    def __str__(self):
        return '(%s) %s' % (self.attack_id, self.name)


class Document(models.Model):
    """Store all documents that can be analyzed to create reports
    """
    docfile = models.FileField()
    created_on = models.DateTimeField(auto_now_add=True)
    updated_on = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)

    def __str__(self):
        return self.docfile.name


class DocumentProcessingJob(models.Model):
    """Queue of document processing jobs
    """
    document = models.ForeignKey(Document, on_delete=models.CASCADE)
    status = models.CharField(max_length=255, default='queued', choices=JOB_STATUS_CHOICES)
    message = models.CharField(max_length=16384, default='')
    created_by = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)
    created_on = models.DateTimeField(auto_now_add=True)
    updated_on = models.DateTimeField(auto_now=True)

    @classmethod
    def create_from_file(cls, f):
        assert isinstance(f, File)
        doc = Document(docfile=f)
        doc.save()
        dpj = DocumentProcessingJob(document=doc)
        dpj.save()
        return dpj

    def __str__(self):
        return 'Process %s' % self.document.docfile.name


class Report(models.Model):
    """Store reports
    """
    name = models.CharField(max_length=200)
    document = models.ForeignKey(Document, null=True, on_delete=models.CASCADE)
    text = models.TextField()
    created_by = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)
    created_on = models.DateTimeField(auto_now_add=True)
    updated_on = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name


class Indicator(models.Model):
    """Indicators extracted from a document for a report
    """
    report = models.ForeignKey(Report, on_delete=models.CASCADE)
    indicator_type = models.CharField(max_length=200)
    value = models.CharField(max_length=200)
    created_on = models.DateTimeField(auto_now_add=True)
    updated_on = models.DateTimeField(auto_now=True)

    def __str__(self):
        return '%s: %s' % (self.indicator_type, self.value)


class Sentence(models.Model):
    text = models.TextField()
    document = models.ForeignKey(Document, null=True, on_delete=models.CASCADE)
    order = models.IntegerField(default=1000)  # Sentences with lower numbers are displayed first
    report = models.ForeignKey(Report, on_delete=models.CASCADE)
    # TODO: Disposition should be on the mapping
    disposition = models.CharField(max_length=200, default=None, null=True, blank=True, choices=DISPOSITION_CHOICES)
    created_on = models.DateTimeField(auto_now_add=True)
    updated_on = models.DateTimeField(auto_now=True)

    def __str__(self):
        append = ''
        if len(self.text) > SENTENCE_PREVIEW_CHARS:
            append = '...'
        return self.text[:SENTENCE_PREVIEW_CHARS] + append


class SentenceTechniqueMapping(models.Model):
    """Maps sentences to ATT&CK TTPs
    """
    report = models.ForeignKey(Report, on_delete=models.CASCADE)  # TODO: Is this field redundant?
    sentence = models.ForeignKey(Sentence, on_delete=models.CASCADE)
    attack_technique = models.ForeignKey(AttackTechnique, on_delete=models.CASCADE, blank=True, null=True)
    confidence = models.FloatField()
    created_on = models.DateTimeField(auto_now_add=True)
    updated_on = models.DateTimeField(auto_now=True)

    def __str__(self):
        return 'Sentence "%s" to %s' % (self.sentence, self.attack_technique)

    @classmethod
    def get_accepted_mappings(cls):
        # Get Attack techniques that have the required amount of positive examples
        attack_techniques = AttackTechnique.get_sentence_counts(accept_threshold=config.ML_ACCEPT_THRESHOLD)
        # Get mappings for the attack techniques above threshold
        mappings = cls.objects.filter(attack_technique__in=attack_techniques)
        return mappings

    @staticmethod
    def from_mapping(mapping_input, mapping):
        # Mapping input is a Sentence
        m = SentenceTechniqueMapping()
        m.report = mapping_input.report
        m.sentence = mapping_input
        at = AttackTechnique.objects.get(attack_id=mapping.attack_technique)
        m.attack_technique = at
        m.confidence = mapping.confidence
        return m


class SentenceGroupMapping(models.Model):
    """Maps sentences to ATT&CK Groups
    """
    report = models.ForeignKey(Report, on_delete=models.CASCADE)  # TODO: Is this field redundant?
    sentence = models.ForeignKey(Sentence, on_delete=models.CASCADE)
    attack_group = models.ForeignKey(AttackGroup, on_delete=models.CASCADE, blank=True, null=True)
    confidence = models.FloatField()
    created_on = models.DateTimeField(auto_now_add=True)
    updated_on = models.DateTimeField(auto_now=True)

    def __str__(self):
        return 'Sentence "%s" to %s' % (self.sentence, self.attack_group)

    @classmethod
    def get_accepted_mappings(cls):
        # Get Attack groups that have the required amount of positive examples
        attack_groups = AttackGroup.get_sentence_counts(accept_threshold=config.ML_ACCEPT_THRESHOLD)
        # Get mappings for the attack techniques above threshold
        mappings = cls.objects.filter(attack_group__in=attack_groups)
        return mappings


class ReportTechniqueMapping(models.Model):
    """
    Maps full reports to ATT&CK TTPs
    """
    report = models.ForeignKey(Report, on_delete=models.CASCADE)
    attack_technique = models.ForeignKey(AttackTechnique, on_delete=models.CASCADE)
    confidence = models.FloatField()
    created_on = models.DateTimeField(auto_now_add=True)
    updated_on = models.DateTimeField(auto_now=True)

    def __str__(self):
        return 'Report "%s" to %s' % (self.report, self.attack_technique)

    @classmethod
    def get_accepted_mappings(cls):
        # Get Attack techniques that have the required amount of positive examples
        attack_techniques = AttackTechnique.get_report_counts(accept_threshold=config.ML_ACCEPT_THRESHOLD)
        # Get mappings for the attack techniques above threshold
        mappings = cls.objects.filter(attack_technique__in=attack_techniques)
        return mappings


class ReportGroupMapping(models.Model):
    """
    Maps full reports to ATT&CK Groups
    """
    report = models.ForeignKey(Report, on_delete=models.CASCADE)
    attack_group = models.ForeignKey(AttackGroup, on_delete=models.CASCADE)
    confidence = models.FloatField()
    created_on = models.DateTimeField(auto_now_add=True)
    updated_on = models.DateTimeField(auto_now=True)

    def __str__(self):
        return 'Report "%s" to %s' % (self.report, self.attack_group)

    @classmethod
    def get_accepted_mappings(cls):
        # Get Attack groups that have the required amount of positive examples
        attack_groups = AttackGroup.get_report_counts(accept_threshold=config.ML_ACCEPT_THRESHOLD)
        # Get mappings for the attack techniques above threshold
        mappings = cls.objects.filter(attack_group__in=attack_groups)
        return mappings


def _delete_file(path):
    # Deletes file from filesystem
    if os.path.isfile(path):
        os.remove(path)


@receiver(post_delete, sender=Document)
def delete_file_post_delete(sender, instance, *args, **kwargs):
    if instance.docfile:
        _delete_file(instance.docfile.path)
