from corehq import privileges
from django_prbac.exceptions import PermissionDenied
from django_prbac.utils import ensure_request_has_privilege
from toggles import StaticToggle, NAMESPACE_DOMAIN


class FeaturePreview(StaticToggle):
    """
    FeaturePreviews should be used in conjunction with normal role based access.
    Check the FeaturePreview first since that's a faster operation.

    e.g.

    if feature_previews.BETA_FEATURE.enabled(domain):
        try:
            ensure_request_has_privilege(request, privileges.BETA_FEATURE)
        except PermissionDenied:
            pass
        else:
            # do cool thing for BETA_FEATURE
    """
    def __init__(self, slug, label, description, privilege=None, help_link=None):
        self.description = description
        self.help_link = help_link
        self.privilege = privilege
        super(FeaturePreview, self).__init__(slug, label, namespaces=[NAMESPACE_DOMAIN])

    def has_privilege(self, request):
        if not self.privilege:
            return True

        try:
            ensure_request_has_privilege(request, self.privilege)
            return True
        except PermissionDenied:
            return False

DEMO_FEATURE_PREVIEW = FeaturePreview(
    'demo_preview',
    'Demo preview',
    'desciption'
)

DEMO_FEATURE_PREVIEW1 = FeaturePreview(
    'demo_preview1',
    'Demo preview with long description',
    'Phasellus sollicitudin magna sed velit semper volutpat. Etiam commodo nisl id luctus fringilla. '
    'Pellentesque habitant morbi tristique senectus et netus et malesuada fames ac turpis egestas. '
    'Morbi varius eu quam vel euismod. Praesent a lacus in mauris cursus sagittis sit amet non ante. '
    'Praesent ullamcorper elit sed nunc luctus malesuada. Praesent ornare mi sapien, quis pretium dui vehicula ut. '
    'Duis ac laoreet ligula, vel hendrerit mauris. Nulla at nisl at ligula aliquam pretium et et augue. '
    'Sed at tincidunt sem, in consequat odio. Proin et quam commodo, ornare quam id, mattis arcu. '
    'Aenean mattis urna quis enim lobortis porta. Duis.',
    privilege=privileges.LOOKUP_TABLES,
    help_link='https://confluence.dimagi.com/display/SPEC/Feature+Preiview+aka+Labs+Specification'
)