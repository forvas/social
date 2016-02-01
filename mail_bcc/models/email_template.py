# -*- coding: utf-8 -*-
################################################################
#    License, author and contributors information in:          #
#    __openerp__.py file at the root folder of this module.    #
################################################################

from openerp import models, fields, SUPERUSER_ID, tools
import openerp
import base64
import logging
_logger = logging.getLogger(__name__)


class EmailTemplate(models.Model):
    _inherit = 'email.template'

    email_bcc = fields.Char(string='Bcc',
                            help='Blind carbon copy message recipients')

    def generate_email_batch(self, cr, uid, template_id, res_ids, context=None, fields=None):
        """Generates an email from the template for given the given model based on
        records given by res_ids.

        :param template_id: id of the template to render.
        :param res_id: id of the record to use for rendering the template (model
                       is taken from template definition)
        :returns: a dict containing all relevant fields for creating a new
                  mail.mail entry, with one extra key ``attachments``, in the
                  format [(report_name, data)] where data is base64 encoded.
        """
        if context is None:
            context = {}
        if fields is None:
            fields = ['subject', 'body_html', 'email_from', 'email_to', 'partner_to', 'email_cc', 'email_bcc', 'reply_to']
        report_xml_pool = self.pool.get('ir.actions.report.xml')
        res_ids_to_templates = self.get_email_template_batch(cr, uid, template_id, res_ids, context)

        # templates: res_id -> template; template -> res_ids
        templates_to_res_ids = {}
        for res_id, template in res_ids_to_templates.iteritems():
            templates_to_res_ids.setdefault(template, []).append(res_id)

        results = dict()
        for template, template_res_ids in templates_to_res_ids.iteritems():
            # generate fields value for all res_ids linked to the current template
            ctx = context.copy()
            if template.lang:
                ctx['lang'] = template._context.get('lang')
            for field in fields:
                generated_field_values = self.render_template_batch(
                    cr, uid, getattr(template, field), template.model, template_res_ids,
                    post_process=(field == 'body_html'),
                    context=ctx)
                for res_id, field_value in generated_field_values.iteritems():
                    results.setdefault(res_id, dict())[field] = field_value
            # compute recipients
            results = self.generate_recipients_batch(cr, uid, results, template.id, template_res_ids, context=context)
            # update values for all res_ids
            for res_id in template_res_ids:
                values = results[res_id]
                # body: add user signature, sanitize
                if 'body_html' in fields and template.user_signature:
                    signature = self.pool.get('res.users').browse(cr, uid, uid, context).signature
                    if signature:
                        values['body_html'] = tools.append_content_to_html(values['body_html'], signature, plaintext=False)
                if values.get('body_html'):
                    values['body'] = tools.html_sanitize(values['body_html'])
                # technical settings
                values.update(
                    mail_server_id=template.mail_server_id.id or False,
                    auto_delete=template.auto_delete,
                    model=template.model,
                    res_id=res_id or False,
                    attachment_ids=[attach.id for attach in template.attachment_ids],
                )

            # Add report in attachments: generate once for all template_res_ids
            if template.report_template:
                for res_id in template_res_ids:
                    attachments = []
                    report_name = self.render_template(cr, uid, template.report_name, template.model, res_id, context=ctx)
                    report = report_xml_pool.browse(cr, uid, template.report_template.id, context)
                    report_service = report.report_name

                    if report.report_type in ['qweb-html', 'qweb-pdf']:
                        result, format = self.pool['report'].get_pdf(cr, uid, [res_id], report_service, context=ctx), 'pdf'
                    else:
                        result, format = openerp.report.render_report(cr, uid, [res_id], report_service, {'model': template.model}, ctx)
            
                    # TODO in trunk, change return format to binary to match message_post expected format
                    result = base64.b64encode(result)
                    if not report_name:
                        report_name = 'report.' + report_service
                    ext = "." + format
                    if not report_name.endswith(ext):
                        report_name += ext
                    attachments.append((report_name, result))
                    results[res_id]['attachments'] = attachments

        return results

    def generate_recipients_batch(self, cr, uid, results, template_id, res_ids, context=None):
        """Generates the recipients of the template. Default values can ben generated
        instead of the template values if requested by template or context.
        Emails (email_to, email_cc) can be transformed into partners if requested
        in the context. """
        if context is None:
            context = {}
        template = self.browse(cr, uid, template_id, context=context)

        if template.use_default_to or context.get('tpl_force_default_to'):
            ctx = dict(context, thread_model=template.model)
            default_recipients = self.pool['mail.thread'].message_get_default_recipients(cr, uid, res_ids, context=ctx)
            for res_id, recipients in default_recipients.iteritems():
                results[res_id].pop('partner_to', None)
                results[res_id].update(recipients)

        for res_id, values in results.iteritems():
            partner_ids = values.get('partner_ids', list())
            if context and context.get('tpl_partners_only'):
                mails = tools.email_split(values.pop('email_to', '')) + tools.email_split(values.pop('email_cc', '')) + tools.email_split(values.pop('email_bcc', ''))
                for mail in mails:
                    partner_id = self.pool.get('res.partner').find_or_create(cr, uid, mail, context=context)
                    partner_ids.append(partner_id)
            partner_to = values.pop('partner_to', '')
            if partner_to:
                # placeholders could generate '', 3, 2 due to some empty field values
                tpl_partner_ids = [int(pid) for pid in partner_to.split(',') if pid]
                partner_ids += self.pool['res.partner'].exists(cr, SUPERUSER_ID, tpl_partner_ids, context=context)
            results[res_id]['partner_ids'] = partner_ids
        return results